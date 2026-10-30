"""Structural validation of a split: fields, types, ids, dates, ordering, question/history links."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .dates import parse_dataset_date
from .loader import SplitPaths, iter_history_lines, read_jsonl


def validate_history(record: dict) -> list[str]:
    errs = []
    hid = record.get("history_id")
    if not isinstance(hid, str) or not hid:
        return ["history without a history_id"]
    sessions = record.get("sessions")
    if not isinstance(sessions, list):
        return [f"{hid}: sessions missing or not a list"]
    seen, prev = set(), None
    for i, s in enumerate(sessions):
        where = f"{hid} session {i}"
        sid = s.get("session_id") if isinstance(s, dict) else None
        if not isinstance(sid, str) or not sid:
            errs.append(f"{where}: no session_id")
            continue
        if sid in seen:
            errs.append(f"{where}: duplicate session_id {sid}")
        seen.add(sid)
        d = parse_dataset_date(s.get("date", ""))
        if d is None:
            errs.append(f"{where}: date {s.get('date')!r} unparseable or weekday mismatch")
        elif prev and d < prev:
            errs.append(f"{where}: dated before the previous session")
        prev = d or prev
        turns = s.get("turns")
        if not isinstance(turns, list):
            errs.append(f"{where}: turns missing or not a list")
            continue
        for j, t in enumerate(turns):
            if not isinstance(t, dict) or t.get("role") not in ("user", "assistant") or not isinstance(t.get("content"), str):
                errs.append(f"{where} turn {j}: role must be user/assistant and content a string")
    return errs


def validate_split(root: Path, max_errors: int = 50) -> dict:
    """Counts and the first ``max_errors`` problems of a split directory."""
    paths = SplitPaths(root)
    paths.check()
    errors: list[str] = []
    qids = Counter()
    by_history = Counter()
    for q in read_jsonl(paths.questions):
        qids[q.get("question_id")] += 1
        by_history[q.get("history_id")] += 1
        for k in ("question_id", "history_id", "question", "question_date"):
            if not isinstance(q.get(k), str) or not q.get(k):
                errors.append(f"question {q.get('question_id')}: {k} missing")
        if parse_dataset_date(q.get("question_date", "")) is None:
            errors.append(f"question {q.get('question_id')}: question_date unparseable")
    errors += [f"duplicate question_id {k}" for k, v in qids.items() if v > 1]
    histories, sessions = 0, 0
    seen_h = set()
    for hid, line in iter_history_lines(paths.histories):
        rec = json.loads(line)
        histories += 1
        sessions += len(rec.get("sessions") or [])
        if hid in seen_h:
            errors.append(f"duplicate history_id {hid}")
        seen_h.add(hid)
        if len(errors) < max_errors:
            errors += validate_history(rec)
    errors += [f"questions refer to missing history {h}" for h in set(by_history) - seen_h]
    return {"split": str(root), "histories": histories, "sessions": sessions, "questions": sum(qids.values()),
            "questions_per_history_max": max(by_history.values()) if by_history else 0,
            "has_gold": paths.gold.exists(), "errors": errors[:max_errors], "error_count": len(errors)}
