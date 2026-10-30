"""Append-only attempt log and explicit accepted-attempt selection.

Every client result is appended to ``attempts.jsonl`` with who/what/when and an outcome class. ``results.jsonl`` (the
file ``reactor/predictions.py`` reads) is rewritten from the log with exactly one line per question: the last
attempt that reached a final state, else the last attempt. Infrastructure failures are never relabelled as
abstentions here; that is an explicit export policy.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

FINAL = {"answered", "reactor_abstained"}


def classify(result: dict) -> str:
    status = result.get("status")
    if status == "answered":
        return "reactor_abstained" if result.get("abstained") else "answered"
    if status in ("timeout", "closed"):
        return "ambiguous"  # the question may have reached the agent; the client does not retry these
    if status == "refused":
        return "not_delivered" if result.get("forwarded") is False or result.get("code") == "too_large" else "refused"
    return "error"


class AttemptLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # a torn last line after a crash
        return out


def accepted(attempts: list[dict], config_hash: str | None = None) -> dict[str, dict]:
    """Per question: the last final attempt, else the last attempt. Attempts from another configuration are ignored
    when ``config_hash`` is given, so different runs are never silently combined."""
    best: dict[str, dict] = {}
    for a in attempts:
        if a.get("kind") != "ask":
            continue
        if config_hash and a.get("config_hash") != config_hash:
            continue
        qid = a["question_id"]
        cur = best.get(qid)
        if cur is None or a["outcome"] in FINAL or cur["outcome"] not in FINAL:
            best[qid] = a
    return best


def write_results(path: Path, chosen: dict[str, dict], order: list[str]) -> int:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    n = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for qid in order:
            if qid in chosen:
                # ASCII, as the client prints it: predictions.py splits its input with str.splitlines(), which would
                # also split on a raw U+2028 inside an answer or question.
                f.write(json.dumps(chosen[qid]["client"]) + "\n")
                n += 1
    os.replace(tmp, path)
    return n
