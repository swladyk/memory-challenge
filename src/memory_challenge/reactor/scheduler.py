"""Run precomputed payloads through Reactor: one Reactor history per dataset history, one question at a time per agent.

Each agent gets its own worker thread and dataset histories are dealt to agents, so different agents can work in
parallel while every agent stays strictly sequential. A dataset history keeps one Reactor history and is never reset
between its questions (organisers: "you shouldn't reset between questions belonging to the same dataset history").

Timeouts (organisers, 2026-10-31). Latency grows as a history accumulates questions, and a client timeout only ends
the client's wait: Reactor keeps working on the question. So a timed-out question is not retried at once. Its ref and
evidence are kept and the run moves on; the next request in the same history waits until the previous one has cleared
(the client retries ``previous_pending``) and reports the late final answer in ``late_replies``. That answer becomes the
question's result (outcome ``answered_late``), its ``[E#]`` labels mapped against *that* question's passages with the
official client's ``citations()`` and INSUFFICIENT_EVIDENCE check. The timeout attempt stays in the log. When the
history's questions are done, a still-unanswered question gets one ``--text`` ping (``ping_text``) to drain late
replies, then is re-asked ``reask`` time(s) in the same Reactor history; a re-ask that times out gets one more ping.

Refusals: the client already retries refusals the server says were never forwarded (``busy``, ``previous_pending``),
backing off 1-8 s within its timeout. If it still returns one, the scheduler waits ``backoff_s`` (7 s: organisers
suggest 5-10 s) and asks again, up to ``retry_refused`` (3) more times.

Resume. The log keeps each dataset history's Reactor history id. On resume, questions with a final answer are skipped
and a dataset history with questions left continues in its stored Reactor history (timeouts from the earlier run can
still be salvaged there). Only when the server answers ``history_changed`` (the agent has moved to another history
since) is a new Reactor history started, and the reason is logged. A ``history_changed`` in the middle of a run means
another process took the agent over: that agent stops rather than reset someone else's history.

Smoke check (``smoke_check``, on for live runs): the run's first question doubles as the connection check. If its
history cannot be opened, or it ends in an error or refusal rather than an answer or a timeout (wrong URL, key or
agent, ``busy`` because someone else is on the agent), the agent stops there instead of trying every question.

Each team has one agent (organisers, 2026-10-30), so a live run uses one backend and is fully sequential; several
backends are supported only for teams that are given more agents.

Every run also writes the live logs of ``memory_challenge.live`` (``run.json``, ``events.jsonl``,
``stats_history.jsonl``) into the run directory: a "processed" event when a question is sent, one event per attempt,
salvage and ping, and running stats after each. With ``gold`` (dev splits) final answers are graded locally with
score.py's own functions; those scores are unofficial.
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import uuid
from pathlib import Path

from ..live import LiveRun, ReactorTracker
from . import citation_fallback
from .backends import abstained, citations, passages_from
from .checkpoints import FINAL, AttemptLog, accepted, classify, write_results

RETRYABLE = ("busy", "previous_pending")  # refusals the server says never reached the agent (client.py RETRYABLE)


def load_payloads(payload_dir: Path, question_order: list[str] | None = None) -> list[dict]:
    files = sorted(Path(payload_dir).glob("*.json"))
    payloads = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    if question_order:
        pos = {q: i for i, q in enumerate(question_order)}
        payloads.sort(key=lambda p: pos.get(p["question_id"], len(pos)))
    return payloads


def salvage(late: dict, pend: dict, seen_during: str | None, via: str) -> dict:
    """The client's own ``answered`` line for a timed-out question whose final answer came back late."""
    answer = late.get("answer") or ""
    passages = passages_from(pend["evidence_path"])
    cited, unknown = citations(answer, passages)
    timed_out = pend["client"]
    return {"status": "answered", "question_id": pend["question_id"], "history_id": timed_out.get("history_id"),
            "ref": timed_out.get("ref"), "question": timed_out.get("question"),
            "question_date": timed_out.get("question_date"), "answer": answer, "reply": None,
            "cycle": late.get("cycle"), "latency_s": None, "citations": cited, "unknown_citations": unknown,
            "uncited": bool(passages) and not cited, "abstained": abstained(answer), "retries": 0,
            "other_replies": [], "late_replies": [],
            "late_salvage": {"via": via, "seen_during": seen_during, "timed_out_attempt": pend["attempt"],
                             "timed_out_status": timed_out.get("status")}}


class _Agent:
    """One backend's sequential work: its current Reactor history and the timed-out questions awaiting a late final."""

    def __init__(self, backend, ctx: dict):
        self.b, self.ctx = backend, ctx
        self.log, self.tracker, self.run_id = ctx["log"], ctx["tracker"], ctx["run_id"]
        self.rh: str | None = None
        self.reused = False  # continuing a stored history: history_changed then means "gone", not "taken over"
        self.confirmed = False  # the server has accepted a request in self.rh during this run
        self.pending: dict[str, dict] = {}  # ref → timed-out ask still waiting for its late final
        self.refs: dict[str, str] = {}  # ref → question id, every ask by this agent (for logging stray late finals)
        self.stopped = False

    def rec(self, record: dict) -> None:
        self.log.append({"run_id": self.run_id, "agent_id": self.b.agent_id, **record})

    # Reactor histories ------------------------------------------------------------------------------------------
    def open_history(self, hid: str, reason: str, previous: str | None = None) -> bool:
        nh = self.b.new_history()
        self.rec({"kind": "new_history", "dataset_history_id": hid, "reactor_history_id": nh.get("history_id"),
                  "status": nh.get("status"), "error": nh.get("error"), "reason": reason,
                  "previous_reactor_history_id": previous})
        if nh.get("status") != "history":
            if not self.ctx["smoke"]["passed"] and self.ctx["smoke"]["enabled"]:
                self.stop(hid, nh, f"smoke check: the first new history failed ({nh.get('error') or nh.get('status')})")
            return False
        self.rh, self.reused, self.confirmed = nh["history_id"], False, False
        if self.pending:  # a new history wipes late replies: those answers can no longer arrive
            self.rec({"kind": "late_dropped", "dataset_history_id": hid, "refs": sorted(self.pending),
                      "questions": sorted({p["question_id"] for p in self.pending.values()}),
                      "note": "new Reactor history: pending late finals are wiped"})
            self.pending = {}
        return True

    def start(self, hid: str, ps: list[dict]) -> bool:
        stored = self.ctx["stored"].get((hid, self.b.agent_id))
        self.pending = {}
        if stored:
            self.rh, self.reused, self.confirmed = stored, True, False
            self.pending = {r: pend for r, pend in self.ctx["timed_out"].get(stored, {}).items()
                            if pend["question_id"] not in self.ctx["final"]}
            self.rec({"kind": "history_reuse", "dataset_history_id": hid, "reactor_history_id": stored,
                      "pending_late": sorted(self.pending), "note": "resume: continuing the stored Reactor history"})
            return True
        if self.open_history(hid, "first questions of this dataset history in this run directory"):
            return True
        for p in ps:  # no history: every question of this dataset history fails, logged as such
            res = {"status": "error", "question_id": p["question_id"], "error": "new history failed"}
            self.rec({"kind": "ask", "dataset_history_id": hid, "reactor_history_id": None,
                      "question_id": p["question_id"], "attempt": 0, "outcome": "error",
                      "config_hash": self.ctx["config_hash"], "client": res})
            self.tracker.result_event(p["question_id"], hid, 0, res, "error", 0.0, None, final=True)
        return False

    # Late finals ------------------------------------------------------------------------------------------------
    def collect_late(self, res: dict, hid: str, seen_during: str | None, via: str) -> None:
        for late in res.get("late_replies") or []:
            ref = str(late.get("ref") or "").lower()
            pend = self.pending.pop(ref, None)
            if pend and pend["question_id"] not in self.ctx["final"]:
                out = salvage(late, pend, seen_during, via)
                qid = pend["question_id"]
                self.rec({"kind": "ask", "dataset_history_id": hid, "reactor_history_id": self.rh, "question_id": qid,
                          "attempt": pend["attempt"], "ref": ref, "outcome": classify(out), "status": "answered",
                          "salvaged": True, "seen_during": seen_during, "via": via,
                          "payload_sha": pend["payload_sha"], "config_hash": self.ctx["config_hash"], "client": out})
                self.mark_final(qid)
                self.tracker.result_event(qid, hid, pend["attempt"], out, "answered_late", 0.0,
                                          self.ctx["fallback"](pend["payload"], out), final=True,
                                          salvage={"via": via, "seen_during": seen_during})
            else:
                self.rec({"kind": "late_reply", "reactor_history_id": self.rh, "ref": ref,
                          "for_question": self.refs.get(ref), "seen_during": seen_during, "via": via,
                          "note": "logged only: its question already has an answer or was not asked in this history"})

    def mark_final(self, qid: str) -> None:
        self.ctx["final"].add(qid)
        for r in [r for r, pend in self.pending.items() if pend["question_id"] == qid]:
            del self.pending[r]

    def ping(self, hid: str, reason: str) -> None:
        t0 = time.perf_counter()
        res = self.b.text(self.rh, self.ctx["ping_text"])
        wall = time.perf_counter() - t0
        n = len(res.get("late_replies") or [])
        self.rec({"kind": "ping", "dataset_history_id": hid, "reactor_history_id": self.rh, "reason": reason,
                  "status": res.get("status"), "code": res.get("code"), "late_replies": n,
                  "reactor_latency_s": res.get("latency_s"), "wall_s": round(wall, 3)})
        self.tracker.ping_event(hid, reason, res.get("status"), n, wall)
        if res.get("code") == "history_changed":
            self.stop(hid, res)
            return
        self.collect_late(res, hid, None, "ping")

    TAKEN_OVER = ("history_changed during the run: another process is using this agent; not starting a new history "
                  "over it. Resume when the agent is free.")

    def stop(self, hid: str, res: dict, reason: str = TAKEN_OVER) -> None:
        self.stopped = True
        self.ctx["stop_reason"].append(reason)
        self.rec({"kind": "stopped", "dataset_history_id": hid, "reactor_history_id": self.rh,
                  "current_history_id": res.get("current_history_id"), "status": res.get("status"),
                  "code": res.get("code"), "reason": reason})

    # Questions --------------------------------------------------------------------------------------------------
    def ask(self, hid: str, p: dict, attempt: int, reask: bool = False) -> int:
        """Asks until the question is settled for now (answered, failed, or timed out and left pending); returns the
        last attempt number."""
        qid = p["question_id"]
        ev_path = self.ctx["evidence_dir"] / f"{qid}.json"
        ev_path.write_text(json.dumps(p["evidence"], ensure_ascii=False), encoding="utf-8")
        payload_sha = hashlib.sha256(ev_path.read_bytes() + p["question_text"].encode()).hexdigest()[:16]
        extra_refused = 0
        while True:
            attempt += 1
            self.tracker.sent_event(qid, hid, attempt, self.b.agent_id)
            t0 = time.perf_counter()
            res = self.b.ask(self.rh, p["question_text"], p["question_date"], ev_path, qid, p["max_chars"])
            wall = time.perf_counter() - t0
            res.setdefault("question_id", qid)
            outcome = classify(res)
            if res.get("ref"):
                self.refs[str(res["ref"]).lower()] = qid
            # the late finals first, so a salvaged answer is logged before this attempt's own record
            gone = res.get("code") == "history_changed"
            if not gone:
                self.collect_late(res, hid, qid, "ask")
            self.rec({"kind": "ask", "dataset_history_id": hid, "reactor_history_id": self.rh, "question_id": qid,
                      "attempt": attempt, "reask": reask, "ref": res.get("ref"), "outcome": outcome,
                      "status": res.get("status"), "code": res.get("code"), "client_retries": res.get("retries"),
                      "reactor_latency_s": res.get("latency_s"), "wall_s": round(wall, 3), "payload_sha": payload_sha,
                      "config_hash": self.ctx["config_hash"], "client": res})
            if gone:
                if self.reused and not self.confirmed:  # the stored history is no longer the agent's: start anew
                    self.tracker.result_event(qid, hid, attempt, res, outcome, wall, None, final=False)
                    old = self.rh
                    if self.open_history(hid, f"stored Reactor history {old} was replaced (history_changed, agent now "
                                              f"on {res.get('current_history_id')}): QUICKSTART allows a new one",
                                         previous=old):
                        continue
                    self.tracker.result_event(qid, hid, attempt, res, "error", wall, None, final=True)
                    return attempt
                self.stop(hid, res)
                self.tracker.result_event(qid, hid, attempt, res, outcome, wall, None, final=True)
                return attempt
            self.confirmed = True
            if outcome in FINAL:
                self.mark_final(qid)
            elif qid in self.ctx["final"]:  # this attempt failed, but its earlier attempt's late final just arrived
                self.tracker.result_event(qid, hid, attempt, res, outcome, wall, None, final=False)
                return attempt
            if outcome == "not_delivered" and res.get("code") in RETRYABLE and extra_refused < self.ctx["retry_refused"]:
                extra_refused += 1
                self.tracker.result_event(qid, hid, attempt, res, outcome, wall, None, final=False)
                time.sleep(self.ctx["backoff_s"])
                continue
            if outcome in FINAL or outcome == "ambiguous":
                self.ctx["smoke"]["passed"] = True  # connected, authorised, accepted: the question reached Reactor
            if outcome == "ambiguous" and res.get("ref"):  # Reactor may still answer: keep waiting for its late final
                self.pending[str(res["ref"]).lower()] = {"question_id": qid, "payload": p, "evidence_path": ev_path,
                                                         "attempt": attempt, "client": res, "wall_s": wall,
                                                         "payload_sha": payload_sha, "reask": reask}
                self.tracker.result_event(qid, hid, attempt, res, outcome, wall, None, final=False)
                return attempt
            self.tracker.result_event(qid, hid, attempt, res, outcome, wall, self.ctx["fallback"](p, res), final=True)
            if self.ctx["smoke"]["enabled"] and not self.ctx["smoke"]["passed"]:
                self.stop(hid, res, f"smoke check: the first question ended in {outcome} "
                                    f"({res.get('code') or res.get('error') or res.get('status')}); nothing else asked")
            return attempt

    def unresolved(self) -> dict[str, dict]:
        """Timed-out questions still without an answer: the latest pending attempt per question."""
        out: dict[str, dict] = {}
        for pend in self.pending.values():
            if pend["question_id"] not in self.ctx["final"]:
                cur = out.get(pend["question_id"])
                if cur is None or pend["attempt"] > cur["attempt"]:
                    out[pend["question_id"]] = pend
        return out

    def run_history(self, hid: str, ps: list[dict]) -> None:
        if not self.start(hid, ps):
            return
        attempts = self.ctx["attempts"]
        for p in ps:
            if self.stopped:
                return
            if p["question_id"] in self.ctx["final"]:  # salvaged meanwhile (a resumed history's late final)
                continue
            attempts[p["question_id"]] = self.ask(hid, p, attempts.get(p["question_id"], 0))
        if self.stopped:
            return
        if self.unresolved():
            self.ping(hid, "drain late finals before re-asking timed-out questions")
        for _ in range(self.ctx["reask"]):
            todo = self.unresolved()
            if self.stopped or not todo:
                break
            for qid, pend in todo.items():
                if self.stopped:
                    return
                if qid not in self.ctx["final"]:
                    attempts[qid] = self.ask(hid, pend["payload"], attempts.get(qid, pend["attempt"]), reask=True)
            if not self.stopped and any(p["reask"] for p in self.unresolved().values()):
                self.ping(hid, "drain late finals after re-asking")
        if self.stopped:
            return
        for qid, pend in self.unresolved().items():  # gave up waiting: the timeout stands
            self.tracker.result_event(qid, hid, pend["attempt"], pend["client"], "ambiguous", pend["wall_s"], None,
                                      final=True)


def _stored_histories(attempts: list[dict]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for a in attempts:
        if a.get("kind") == "new_history" and a.get("status") == "history" and a.get("reactor_history_id"):
            out[(a["dataset_history_id"], a["agent_id"])] = a["reactor_history_id"]
    return out


def run(payloads: list[dict], backends: list, run_dir: Path, retry_refused: int = 3, backoff_s: float = 7.0,
        reask: int = 1, ping_text: str = "Reply only OK.", gold: dict | None = None,
        live_meta: dict | None = None, smoke_check: bool = False) -> dict:
    run_dir = Path(run_dir)
    log = AttemptLog(run_dir / "attempts.jsonl")
    hashes = {p["config_hash"] for p in payloads}
    if len(hashes) > 1:
        raise ValueError(f"payloads come from {len(hashes)} different configurations; run them separately")
    config_hash = next(iter(hashes)) if hashes else None
    history = log.load()
    done = {q for q, a in accepted(history, config_hash).items() if a["outcome"] in FINAL}
    groups: dict[str, list[dict]] = {}
    for p in payloads:
        if p["question_id"] not in done:
            groups.setdefault(p["dataset_history_id"], []).append(p)
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    by_qid = {p["question_id"]: p for p in payloads}
    timed_out: dict[str, dict[str, dict]] = {}  # Reactor history → ref → an earlier run's timeout, for salvage
    attempts: dict[str, int] = {}
    for a in history:
        if a.get("kind") == "ask" and a.get("question_id") in by_qid:
            attempts[a["question_id"]] = max(attempts.get(a["question_id"], 0), a.get("attempt") or 0)
            if a.get("outcome") == "ambiguous" and a.get("ref") and a.get("reactor_history_id"):
                p = by_qid[a["question_id"]]
                timed_out.setdefault(a["reactor_history_id"], {})[str(a["ref"]).lower()] = {
                    "question_id": p["question_id"], "payload": p, "evidence_path": evidence_dir / f"{p['question_id']}.json",
                    "attempt": a.get("attempt") or 0, "client": a.get("client") or {}, "wall_s": a.get("wall_s") or 0.0,
                    "payload_sha": a.get("payload_sha"), "reask": False}
    run_id = uuid.uuid4().hex[:12]
    log.append({"kind": "run_start", "run_id": run_id, "config_hash": config_hash, "pending_questions":
                sum(map(len, groups.values())), "pending_histories": len(groups), "skipped_done": len(done),
                "agents": [b.agent_id for b in backends], "reask": reask})
    work: queue.Queue = queue.Queue()
    for hid, ps in groups.items():
        work.put((hid, ps))
    live = LiveRun(run_dir, "reactor", {**(live_meta or {}), "config_hash": config_hash, "scheduler_run_id": run_id,
                                        "skipped_done": len(done), "histories": len(groups),
                                        "agents": [b.agent_id for b in backends]})
    tracker = ReactorTracker(live, sum(map(len, groups.values())), gold)

    def fallback_preview(p: dict, res: dict) -> list[str] | None:
        """Sessions the supported-citation fallback would cite for an uncited answer (export decides; this only logs)."""
        if res.get("status") != "answered" or res.get("abstained") or res.get("citations"):
            return None
        own = {e["id"] for e in p["evidence"]}
        return citation_fallback.choose(res.get("answer") or "", {"evidence": p["evidence"]}, own)[0]

    ctx = {"log": log, "tracker": tracker, "run_id": run_id, "config_hash": config_hash, "evidence_dir": evidence_dir,
           "stored": _stored_histories(history), "timed_out": timed_out, "final": set(done), "attempts": attempts,
           "retry_refused": retry_refused, "backoff_s": backoff_s, "reask": reask, "ping_text": ping_text,
           "fallback": fallback_preview, "smoke": {"enabled": smoke_check, "passed": False}, "stop_reason": []}

    def worker(backend):
        agent = _Agent(backend, ctx)
        while not agent.stopped:
            try:
                hid, ps = work.get_nowait()
            except queue.Empty:
                return
            agent.run_history(hid, ps)

    threads = [threading.Thread(target=worker, args=(b,), daemon=True) for b in backends]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    except BaseException:
        live.finish("failed")
        raise
    chosen = accepted(log.load(), config_hash)
    order = [p["question_id"] for p in payloads]
    n = write_results(run_dir / "results.jsonl", chosen, order)
    counts: dict[str, int] = {}
    for a in chosen.values():
        counts[a["outcome"]] = counts.get(a["outcome"], 0) + 1
    stopped = "; ".join(ctx["stop_reason"]) or None
    summary = {"run_id": run_id, "results_written": n, "outcomes": counts, "questions": len(order), "stopped": stopped}
    log.append({"kind": "run_end", **summary})
    live.finish("stopped" if stopped else "finished", outcomes=counts, results_written=n, stopped=stopped)
    return summary
