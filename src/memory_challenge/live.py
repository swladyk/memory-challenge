"""Live, append-only run logs for the dashboard's Live evaluation view and later analysis.

Each run directory gets:

- ``run.json``: what the run is (kind, config, split, totals, status), rewritten atomically;
- ``events.jsonl``: one line per question-level event (retrieval: per-question metrics; Reactor: sent/result);
- ``stats_history.jsonl``: one running snapshot after every question: staged progress (processed / evaluated /
  graded out of total), elapsed, ETA, and cumulative overall and per-type metrics with latency percentiles.

Lines are appended, flushed and fsynced as they happen. They hold ids, labels and numbers only: no conversation text,
no answers, no secrets. Reactor-run scores computed here mirror ``score.py`` locally and are labelled unofficial.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import statistics
import threading
import time
from collections import defaultdict
from pathlib import Path

from .evaluation.metrics import summarize

SCHEMA = "memret.live.v1"
RETRIEVAL_KEYS = ("recall@5", "recall@10", "all@10", "all@50", "pack_recall", "pack_all", "pack_precision", "pack_f1",
                  "answer_text_in_pack", "passages_mean", "message_chars_mean")


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, max(0, round(p / 100 * (len(v) - 1))))], 3)


class LiveRun:
    def __init__(self, run_dir: Path, kind: str, meta: dict):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.started = time.time()
        self._lock = threading.Lock()
        events = self.dir / "events.jsonl"  # a resumed run appends: keep seq increasing
        self._seq = 0
        if events.exists():
            with open(events, "rb") as f:
                self._seq = sum(1 for _ in f)
        self.meta = {"schema": SCHEMA, "run_id": self.dir.name, "kind": kind, "status": "running",
                     "created": self.started, **meta}
        self._write_meta()

    def _write_meta(self) -> None:
        tmp = self.dir / "run.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=1), encoding="utf-8")
        tmp.replace(self.dir / "run.json")

    def _append(self, name: str, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=True) + "\n"
        with open(self.dir / name, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def event(self, record: dict) -> None:
        with self._lock:
            self._seq += 1
            self._append("events.jsonl", {"seq": self._seq, "ts": round(time.time(), 3), "run_id": self.dir.name,
                                          **record})

    def snapshot(self, record: dict) -> None:
        with self._lock:
            self._append("stats_history.jsonl", {"ts": round(time.time(), 3), "run_id": self.dir.name, **record})

    def update(self, **fields) -> None:
        with self._lock:
            self.meta.update(fields)
            self._write_meta()

    def finish(self, status: str = "finished", **fields) -> None:
        self.update(status=status, finished=time.time(), elapsed_s=round(time.time() - self.started, 1), **fields)


def _progress(total: int, processed: int, evaluated: int, graded: int | None, started: float) -> dict:
    elapsed = time.time() - started
    rate = processed / elapsed if elapsed > 0 else 0.0
    eta = (total - processed) / rate if rate > 0 and total > processed else 0.0
    return {"total": total, "processed": processed, "evaluated": evaluated, "graded": graded,
            "elapsed_s": round(elapsed, 2), "eta_s": round(eta, 1), "rate_per_min": round(rate * 60, 2)}


class RetrievalTracker:
    """Per-question events and running snapshots for retrieval-only runs (``evaluate-retrieval``, ``retrieve``)."""

    def __init__(self, live: LiveRun, total: int, has_gold: bool):
        self.live, self.total, self.has_gold = live, total, has_gold
        self.processed = self.evaluated = 0
        self.scored: list[dict] = []
        self.raw: list[dict] = []
        live.update(total=total, has_gold=has_gold)

    def add(self, rec: dict, scored: dict | None) -> None:
        self.processed += 1  # retrieval and packing done: the payload exists
        lat = {"index_build_ms": round(rec["index_build_s"] * 1000, 2) if rec.get("cold") else None,
               "retrieval_ms": round(rec["retrieval_s"] * 1000, 2), "pack_ms": round(rec["pack_s"] * 1000, 2)}
        self.raw.append({"passages": rec["passages"], "message_chars": rec["message_chars"], **lat})
        self.evaluated += 1  # per-question metrics computed (sizes, latencies)
        event = {"stage": "graded" if scored else "evaluated", "question_id": rec["question_id"],
                 "history_id": rec["history_id"], "hints": rec.get("hints", []), "passages": rec["passages"],
                 "message_chars": rec["message_chars"], "latency": lat}
        if scored:
            self.scored.append(scored)
            event["type"] = scored["type"]
            event["abstain"] = scored["abstain"]
            event["metrics"] = {k: scored[k] for k in scored if k.startswith(("recall@", "all@", "pack_", "answer_text"))}
        self.live.event(event)
        self.live.snapshot(self.snapshot())

    def snapshot(self) -> dict:
        snap = {"progress": _progress(self.total, self.processed, self.evaluated,
                                      len(self.scored) if self.has_gold else None, self.live.started)}
        lat = {k: [r[k] for r in self.raw if r[k] is not None] for k in ("index_build_ms", "retrieval_ms", "pack_ms")}
        snap["latency_ms"] = {f"{k[:-3]}_p{p}": _pct(v, p) for k, v in lat.items() for p in (50, 95)}
        snap["size"] = {"passages_mean": round(statistics.fmean(r["passages"] for r in self.raw), 2),
                        "message_chars_mean": round(statistics.fmean(r["message_chars"] for r in self.raw), 1)}
        if self.scored:
            s = summarize(self.scored)
            snap["metrics"] = {t: {"n": row["n"], "answerable": row["answerable"],
                                   **{k: row.get(k) for k in RETRIEVAL_KEYS}}
                               for t, row in s.items() if not t.startswith("_")}
        return snap


def load_scorer():
    """The repository's score.py (standard library), for local per-question grading of Reactor answers."""
    path = Path(__file__).resolve().parents[2] / "score.py"
    spec = importlib.util.spec_from_file_location("memret_official_score", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LABELS = re.compile(r"\s*\[\s*E\d+(?:\s*[,;]\s*E\d+)*\s*\]")
ANSWERED = ("answered", "reactor_abstained", "answered_late")  # = reactor.checkpoints.FINAL


class ReactorTracker:
    """Per-attempt events and running snapshots for Reactor runs; with gold, local score.py-equivalent grading."""

    def __init__(self, live: LiveRun, total: int, gold: dict | None):
        self.live, self.total, self.gold = live, total, gold
        self.scorer = load_scorer() if gold else None
        self.sent: set[str] = set()
        self.final: dict[str, dict] = {}
        self.graded: dict[str, dict] = {}
        self.latencies: list[float] = []
        self.order = 0  # requests answered or given up on, in the order they were sent
        self.late_salvaged = 0
        self.pings = 0
        self._lock = threading.Lock()
        live.update(total=total, has_gold=bool(gold), scores_label="local, unofficial (score.py-equivalent)")

    def sent_event(self, question_id: str, history: str, attempt: int, agent: str) -> None:
        with self._lock:
            self.sent.add(question_id)
            self.live.event({"stage": "processed", "question_id": question_id, "dataset_history_id": history,
                             "attempt": attempt, "agent_id": agent})
            self.live.snapshot(self.snapshot())

    def result_event(self, question_id: str, history: str, attempt: int, res: dict, outcome: str, wall_s: float,
                     fallback: list[str] | None, final: bool, salvage: dict | None = None) -> None:
        """One request's result (``salvage``: a timed-out question's late final, found in a later request)."""
        with self._lock:
            self.sent.add(question_id)  # also covers questions that failed before they could be sent
            cites = [c.get("id") for c in res.get("citations") or [] if isinstance(c, dict) and c.get("id")]
            event = {"stage": "evaluated" if final else "attempt", "question_id": question_id,
                     "dataset_history_id": history, "attempt": attempt, "outcome": outcome, "status": res.get("status"),
                     "code": res.get("code"), "reactor_latency_s": res.get("latency_s"), "wall_s": round(wall_s, 3),
                     "retries": res.get("retries"), "citations": len(cites), "uncited": bool(res.get("uncited")),
                     "unknown_citations": len(res.get("unknown_citations") or []),
                     "fallback_would_cite": len(fallback) if fallback is not None else None}
            latest = None
            if salvage:
                self.late_salvaged += 1
                event["late_salvaged"] = salvage
            elif wall_s > 0:  # a request that went out: per-request latency in send order (growth over a history)
                self.order += 1
                latest = {"order": self.order, "question_id": question_id, "attempt": attempt, "outcome": outcome,
                          "reactor_latency_s": res.get("latency_s"), "wall_s": round(wall_s, 2)}
            if final:
                self.final[question_id] = {"outcome": outcome}
                if isinstance(res.get("latency_s"), (int, float)):
                    self.latencies.append(float(res["latency_s"]))
                if self.gold and question_id in self.gold:
                    self.graded[question_id] = g = self._grade(question_id, res, outcome, cites, fallback)
                    event["stage"] = "graded"
                    event["local_unofficial"] = g
            self.live.event(event)
            snap = self.snapshot()
            if latest:
                snap["latest_request"] = latest
            self.live.snapshot(snap)

    def ping_event(self, history: str, reason: str, status: str | None, late_replies: int, wall_s: float) -> None:
        with self._lock:
            self.pings += 1
            self.live.event({"stage": "ping", "dataset_history_id": history, "reason": reason, "status": status,
                             "late_replies": late_replies, "wall_s": round(wall_s, 3)})
            self.live.snapshot(self.snapshot())

    def _grade(self, qid: str, res: dict, outcome: str, cites: list[str], fallback: list[str] | None) -> dict:
        gold = self.gold[qid]
        if outcome in ANSWERED:
            abstain = bool(res.get("abstained"))
            pred = {"abstain": abstain, "answer": _LABELS.sub("", res.get("answer") or "").strip(),
                    "citations": [] if abstain else list(dict.fromkeys(cites))}
        else:
            pred = None  # no answer: scored as missing, i.e. wrong
        row = self.scorer.score_question(pred, gold)
        out = {"type": gold["question_type"], "answer": row["answer"], "abstain_ok": row["abstain_ok"],
               "gold_abstain": gold["abstain"], "evidence_f1": row["evidence_f1"]}
        if fallback is not None and pred and not pred["abstain"] and not pred["citations"] and not gold["abstain"]:
            out["evidence_f1_with_fallback"] = self.scorer.evidence_f1(fallback, gold["evidence"])
        return out

    def snapshot(self) -> dict:
        outcomes = defaultdict(int)
        for f in self.final.values():
            outcomes[f["outcome"]] += 1
        failures = sum(v for k, v in outcomes.items() if k not in ANSWERED)
        snap = {"progress": {**_progress(self.total, len(self.sent), len(self.final),
                                         len(self.graded) if self.gold else None, self.live.started),
                             "failures": failures, "late_salvaged": self.late_salvaged, "pings": self.pings},
                "outcomes": dict(outcomes),
                "reactor_latency_s": {"mean": round(statistics.fmean(self.latencies), 2) if self.latencies else None,
                                      "p50": _pct(self.latencies, 50), "p95": _pct(self.latencies, 95)}}
        if self.graded:
            groups: dict[str, list[dict]] = defaultdict(list)
            for g in self.graded.values():
                groups[g["type"]].append(g)
                groups["all"].append(g)
            table = {}
            for t, rows in groups.items():
                ans = [r for r in rows if not r["gold_abstain"]]
                unans = [r for r in rows if r["gold_abstain"]]
                f1 = [r["evidence_f1"] for r in ans if r["evidence_f1"] is not None]
                fb = [r.get("evidence_f1_with_fallback", r["evidence_f1"]) for r in ans if r["evidence_f1"] is not None]
                table[t] = {"n": len(rows),
                            "correct": round(sum(r["answer"] == "correct" for r in rows) / len(rows), 4),
                            "unjudged": round(sum(r["answer"] == "unjudged" for r in rows) / len(rows), 4),
                            "abstention_recall": round(sum(r["abstain_ok"] for r in unans) / len(unans), 4) if unans else None,
                            "false_abstention": round(sum(not r["abstain_ok"] for r in ans) / len(ans), 4) if ans else None,
                            "evidence_f1": round(statistics.fmean(f1), 4) if f1 else None,
                            "evidence_f1_with_fallback": round(statistics.fmean(fb), 4) if fb else None}
            snap["metrics"] = table
            snap["metrics_label"] = "local, unofficial: answers score.py cannot match are 'unjudged'"
        return snap
