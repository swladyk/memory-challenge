"""Run retrieval over a split, one history per worker process, without gold.

Workers build one ``HistoryIndex`` per history and run every question of that history through the configured
``Retriever``. They return rankings, packed payloads and timings; gold is joined later in ``metrics``. Payloads and
traces are optionally written under the run directory (gitignored: they contain conversation text).
"""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

from ..config import Config
from ..data.loader import SplitPaths, iter_history_lines, load_questions, parse_history
from ..indexes.history_index import HistoryIndex
from ..retrieval.controller import Retriever
from ..schemas import Question


def partition_of(history_id: str, lockbox_share: float = 0.3) -> str:
    """Deterministic tune/lockbox split by history id. dev and dev-large share history ids, and cued/uncued twins share
    a history, so paired questions always land in the same partition."""
    h = int(hashlib.sha256(history_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "lockbox" if h < lockbox_share else "tune"


def payload_record(q: Question, res, cfg: Config) -> dict:
    p = res.pack
    return {"question_id": q.question_id, "dataset_history_id": q.history_id, "question": q.question,
            "question_text": p.question_text, "question_date": q.question_date, "evidence": p.evidence,
            "max_chars": p.limit, "message_chars": p.message_chars, "message_builder": p.builder,
            "manifest": p.manifest, "question_notes": p.question_notes,
            "config_hash": cfg.hash(), "retrieval_mode": res.mode}


def trace_record(q: Question, res, index_seconds: float, cold: bool) -> dict:
    return {"question_id": q.question_id, "plan": res.plan.summary(), "coverage": res.coverage, "notes": res.notes,
            "candidates": [{"window": c.window_id, "session": c.session_id, "score": round(c.score, 6),
                            "strength": round(c.strength, 4), "ranks": c.ranks,
                            "scores": {k: round(v, 4) for k, v in c.scores.items()}, "via": c.via}
                           for c in res.candidates[:60]],
            "excluded": res.pack.excluded[:60], "timings_ms": {k: round(v * 1000, 3) for k, v in res.timings.items()},
            "index_build_ms": round(index_seconds * 1000, 3), "cold": cold, "mode": res.mode}


def run_history(line: str, questions: list[Question], cfg: Config, out_dir: str | None) -> list[dict]:
    t0 = time.perf_counter()
    history = parse_history(json.loads(line))
    index = HistoryIndex(history, cfg)
    index_s = time.perf_counter() - t0
    retriever = Retriever(cfg)
    out = []
    for n, q in enumerate(questions):
        t1 = time.perf_counter()
        res = retriever.run(q, index)
        total = time.perf_counter() - t1
        rec = {"question_id": q.question_id, "history_id": q.history_id,
               "ranked_sessions": res.ranked_sessions()[:50],
               "packed_sessions": [e["id"] for e in res.pack.evidence],
               "packed_text": "\n".join(e["text"] for e in res.pack.evidence),
               "message_chars": res.pack.message_chars, "passages": len(res.pack.evidence),
               "index_build_s": index_s, "retrieval_s": total, "cold": n == 0,
               "pack_s": res.timings.get("pack", 0.0), "timings": res.timings, "hints": list(res.plan.hints),
               "windows_in_history": len(index.windows), "mode": res.mode}
        if out_dir:
            d = Path(out_dir)
            (d / "payloads").mkdir(parents=True, exist_ok=True)
            (d / "traces").mkdir(parents=True, exist_ok=True)
            (d / "payloads" / f"{q.question_id}.json").write_text(json.dumps(payload_record(q, res, cfg), ensure_ascii=False))
            (d / "traces" / f"{q.question_id}.json").write_text(json.dumps(trace_record(q, res, index_s, n == 0), ensure_ascii=False))
        out.append(rec)
    return out


def run_split(split_dir: Path, cfg: Config, workers: int = 4, partition: str = "all", limit: int | None = None,
              out_dir: Path | None = None, question_ids: set[str] | None = None) -> list[dict]:
    paths = SplitPaths(split_dir)
    paths.check()
    by_h: dict[str, list[Question]] = {}
    for q in load_questions(paths.questions):
        if question_ids and q.question_id not in question_ids:
            continue
        if partition != "all" and partition_of(q.history_id) != partition:
            continue
        by_h.setdefault(q.history_id, []).append(q)
    hids = list(by_h)
    if limit and limit < len(hids):  # evenly spaced, so a subset spans every question type (files are type-ordered)
        step = len(hids) / limit
        hids = [hids[int(i * step)] for i in range(limit)]
    wanted = set(hids)
    results: list[dict] = []
    od = str(out_dir) if out_dir else None
    if workers <= 1:
        for hid, line in iter_history_lines(paths.histories, wanted):
            results += run_history(line, by_h[hid], cfg, od)
        return results
    with ProcessPoolExecutor(workers) as pool:
        futs = []
        for hid, line in iter_history_lines(paths.histories, wanted):
            futs.append(pool.submit(run_history, line, by_h[hid], cfg, od))
            while sum(not f.done() for f in futs) >= 2 * workers:  # bound the lines held in memory
                wait([f for f in futs if not f.done()], return_when=FIRST_COMPLETED)
        for f in futs:
            results += f.result()
    return results
