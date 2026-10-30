"""Loading the released splits: histories streamed one line at a time, questions, and (evaluation only) gold.

``SplitPaths`` finds the files; ``iter_histories`` yields ``History`` objects whose session contents are memoised by
(session_id, content hash), so a session reused across histories is parsed into windows once per process while its
dates stay per history. Gold is loaded only by ``load_gold``, which inference code never calls.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..schemas import History, Question, SessionContent, SessionOccurrence, Turn
from .dates import parse_dataset_date


@dataclass(frozen=True)
class SplitPaths:
    root: Path

    @property
    def histories(self) -> Path:
        return self.root / "histories.jsonl"

    @property
    def questions(self) -> Path:
        return self.root / "questions.jsonl"

    @property
    def gold(self) -> Path:
        return self.root / "gold.jsonl"

    def check(self) -> None:
        for p in (self.histories, self.questions):
            if not p.exists():
                raise FileNotFoundError(f"{p} not found: download the split from the release page (README.md:13-15)")


def read_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8-sig") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path} line {n}: not valid JSON ({exc.msg})") from exc


def content_hash(turns: list[dict]) -> str:
    h = hashlib.sha1()
    for t in turns:
        h.update(str(t.get("role", "")).encode() + b"\0" + str(t.get("content", "")).encode() + b"\1")
    return h.hexdigest()


_CONTENT_MEMO: dict[tuple[str, str], SessionContent] = {}


def session_content(session: dict) -> SessionContent:
    turns = session.get("turns") or []
    key = (session["session_id"], content_hash(turns))
    got = _CONTENT_MEMO.get(key)
    if got is None:
        got = SessionContent(key[0], key[1], tuple(
            Turn(str(t.get("turn_id", "")), str(t.get("role", "")), str(t.get("content", "")), i)
            for i, t in enumerate(turns)))
        if len(_CONTENT_MEMO) > 100_000:
            _CONTENT_MEMO.clear()
        _CONTENT_MEMO[key] = got
    return got


def parse_history(record: dict) -> History:
    hid = record["history_id"]
    occurrences, contents = [], {}
    for pos, s in enumerate(record.get("sessions") or []):
        c = session_content(s)
        contents[c.session_id] = c
        occurrences.append(SessionOccurrence(hid, c.session_id, s.get("date", ""), parse_dataset_date(s.get("date", "")), pos))
    return History(hid, occurrences, contents)


def history_id_of(line: str) -> str:
    """The history id of a histories.jsonl line without parsing the whole (multi-MB) line."""
    head = line[:300]
    i = head.find('"history_id"')
    if i < 0:
        return json.loads(line)["history_id"]
    j = head.index('"', head.index(":", i) + 1)
    return head[j + 1: head.index('"', j + 1)]


def iter_history_lines(path: Path, wanted: set[str] | None = None) -> Iterator[tuple[str, str]]:
    """(history_id, raw line) for each history, optionally only those in ``wanted``."""
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            if not line.strip():
                continue
            hid = history_id_of(line)
            if wanted is None or hid in wanted:
                yield hid, line


def iter_histories(path: Path, wanted: set[str] | None = None) -> Iterator[History]:
    for _, line in iter_history_lines(path, wanted):
        yield parse_history(json.loads(line))


def load_questions(path: Path) -> list[Question]:
    out = []
    for r in read_jsonl(path):
        out.append(Question(str(r["question_id"]), str(r["history_id"]), str(r["question"]), str(r["question_date"])))
    return out


def load_gold(path: Path) -> dict[str, dict]:
    """Gold answers and evidence: for evaluation code only."""
    return {g["question_id"]: g for g in read_jsonl(path)}


_FP_MEMO: dict[tuple[str, int, int], str] = {}


def fingerprint(path: Path) -> str:
    """sha256 of a file (memoised by path, size and mtime)."""
    st = os.stat(path)
    key = (str(Path(path).resolve()), st.st_size, int(st.st_mtime))
    if key not in _FP_MEMO:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        _FP_MEMO[key] = h.hexdigest()[:16]
    return _FP_MEMO[key]
