"""Run precomputed payloads through Reactor: one Reactor history per dataset history, one question at a time per agent.

Each agent gets its own worker thread and dataset histories are dealt to agents, so different agents can work in
parallel while every agent stays strictly sequential. A dataset history gets a new Reactor history once per run and is
never reset between its questions. On resume, questions with a final attempt are skipped and any history with
questions left starts a new Reactor history (payloads are self-contained, so nothing must be reloaded).

Retries: the client already retries refusals the server says were never forwarded (``busy``, ``previous_pending``),
backing off 1-8 s within its timeout. If it still returns one, the scheduler waits ``backoff_s`` (7 s: the organisers
expect ``previous_pending`` right after an answer and suggest 5-10 s) and asks again, up to ``retry_refused`` (3) more
times. An ``ambiguous`` outcome (timeout, closed chat: the question may have been delivered) is retried only when
``retry_ambiguous`` is set. ``history_changed`` stops that history: another process is using the agent. Late final
answers for earlier questions are logged, never taken as the current answer.

Each team has one agent (organisers, 2026-10-30), so a live run uses one backend and is fully sequential; several
backends are supported only for teams that are given more agents.
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import uuid
from pathlib import Path

from .checkpoints import FINAL, AttemptLog, accepted, classify, write_results


def load_payloads(payload_dir: Path, question_order: list[str] | None = None) -> list[dict]:
    files = sorted(Path(payload_dir).glob("*.json"))
    payloads = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    if question_order:
        pos = {q: i for i, q in enumerate(question_order)}
        payloads.sort(key=lambda p: pos.get(p["question_id"], len(pos)))
    return payloads


def run(payloads: list[dict], backends: list, run_dir: Path, retry_refused: int = 3, retry_ambiguous: bool = False,
        backoff_s: float = 7.0) -> dict:
    run_dir = Path(run_dir)
    log = AttemptLog(run_dir / "attempts.jsonl")
    hashes = {p["config_hash"] for p in payloads}
    if len(hashes) > 1:
        raise ValueError(f"payloads come from {len(hashes)} different configurations; run them separately")
    config_hash = next(iter(hashes)) if hashes else None
    done = {q for q, a in accepted(log.load(), config_hash).items() if a["outcome"] in FINAL}
    groups: dict[str, list[dict]] = {}
    for p in payloads:
        if p["question_id"] not in done:
            groups.setdefault(p["dataset_history_id"], []).append(p)
    run_id = uuid.uuid4().hex[:12]
    log.append({"kind": "run_start", "run_id": run_id, "config_hash": config_hash, "pending_questions":
                sum(map(len, groups.values())), "pending_histories": len(groups), "skipped_done": len(done),
                "agents": [b.agent_id for b in backends]})
    work: queue.Queue = queue.Queue()
    for hid, ps in groups.items():
        work.put((hid, ps))
    evidence_dir = run_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    def worker(backend):
        refs: dict[str, str] = {}
        while True:
            try:
                hid, ps = work.get_nowait()
            except queue.Empty:
                return
            nh = backend.new_history()
            log.append({"kind": "new_history", "run_id": run_id, "agent_id": backend.agent_id, "dataset_history_id": hid,
                        "reactor_history_id": nh.get("history_id"), "status": nh.get("status"),
                        "error": nh.get("error")})
            if nh.get("status") != "history":
                for p in ps:
                    log.append({"kind": "ask", "run_id": run_id, "agent_id": backend.agent_id, "dataset_history_id": hid,
                                "reactor_history_id": None, "question_id": p["question_id"], "attempt": 0,
                                "outcome": "error", "config_hash": config_hash,
                                "client": {"status": "error", "question_id": p["question_id"],
                                           "error": f"new history failed: {nh.get('error') or nh.get('status')}"}})
                continue
            rh = nh["history_id"]
            for p in ps:
                qid = p["question_id"]
                ev_path = evidence_dir / f"{qid}.json"
                ev_path.write_text(json.dumps(p["evidence"], ensure_ascii=False), encoding="utf-8")
                payload_sha = hashlib.sha256(ev_path.read_bytes() + p["question_text"].encode()).hexdigest()[:16]
                attempt, extra_refused = 0, 0
                stop_history = False
                while True:
                    attempt += 1
                    t0 = time.perf_counter()
                    res = backend.ask(rh, p["question_text"], p["question_date"], ev_path, qid, p["max_chars"])
                    wall = time.perf_counter() - t0
                    res.setdefault("question_id", qid)
                    outcome = classify(res)
                    if res.get("ref"):
                        refs[res["ref"]] = qid
                    log.append({"kind": "ask", "run_id": run_id, "agent_id": backend.agent_id, "dataset_history_id": hid,
                                "reactor_history_id": rh, "question_id": qid, "attempt": attempt, "ref": res.get("ref"),
                                "outcome": outcome, "status": res.get("status"), "code": res.get("code"),
                                "client_retries": res.get("retries"), "reactor_latency_s": res.get("latency_s"),
                                "wall_s": round(wall, 3), "payload_sha": payload_sha, "config_hash": config_hash,
                                "client": res})
                    for late in res.get("late_replies") or []:
                        log.append({"kind": "late_reply", "run_id": run_id, "agent_id": backend.agent_id,
                                    "reactor_history_id": rh, "ref": late.get("ref"),
                                    "for_question": refs.get(late.get("ref")), "seen_during": qid,
                                    "note": "logged only; never taken as the current answer"})
                    if res.get("code") == "history_changed":
                        stop_history = True
                        break
                    if outcome == "not_delivered" and res.get("code") != "too_large" and extra_refused < retry_refused:
                        extra_refused += 1
                        time.sleep(backoff_s)
                        continue
                    if outcome == "ambiguous" and retry_ambiguous and attempt == 1:
                        continue
                    break
                if stop_history:
                    break

    threads = [threading.Thread(target=worker, args=(b,), daemon=True) for b in backends]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    chosen = accepted(log.load(), config_hash)
    order = [p["question_id"] for p in payloads]
    n = write_results(run_dir / "results.jsonl", chosen, order)
    counts: dict[str, int] = {}
    for a in chosen.values():
        counts[a["outcome"]] = counts.get(a["outcome"], 0) + 1
    summary = {"run_id": run_id, "results_written": n, "outcomes": counts, "questions": len(order)}
    log.append({"kind": "run_end", **summary})
    return summary
