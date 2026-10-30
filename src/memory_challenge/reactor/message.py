"""The exact message the official client sends, to size payloads against ``--max-chars``.

Prefers the kit's own ``build_message`` and ``parse_question_date`` (``reactor/client.py``, loaded read-only and
unmodified). If that module can't be loaded, a string-for-string mirror of ``client.py:169-246`` is used, and
``tests/test_message.py`` checks the mirror against the official function whenever the official one is importable.
Passages are normalised exactly as ``load_evidence`` does (``client.py:187-215``): text and date stripped.
"""
from __future__ import annotations

import importlib.util
import os
import re
from datetime import datetime
from pathlib import Path

REF = "0" * 8  # the client's ref is always 8 hex characters
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
_DATE = re.compile(r"^(\d{4})([-/])(\d{2})\2(\d{2})(?: \(([A-Za-z]{3})\))?(?: (\d{2}):(\d{2}))?$")
_official = None
_official_tried = False


def client_path() -> Path:
    env = os.environ.get("MEMRET_CLIENT_PATH")
    return Path(env) if env else Path(__file__).resolve().parents[3] / "reactor" / "client.py"


def official():
    """The kit's client module, or None if it can't be loaded."""
    global _official, _official_tried
    if not _official_tried:
        _official_tried = True
        path = client_path()
        try:
            spec = importlib.util.spec_from_file_location("memret_official_reactor_client", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _official = mod if hasattr(mod, "build_message") else None
        except Exception:  # missing file, syntax or import problem: fall back to the mirror
            _official = None
    return _official


def mirror_parse_question_date(value: str) -> str:
    m = _DATE.match(value or "")
    if not m:
        raise ValueError(f"--question-date {value!r}: use YYYY-MM-DD, YYYY-MM-DD HH:MM or 2023/05/30 (Tue) 08:08")
    year, month, day, weekday, hour, minute = m.group(1, 3, 4, 5, 6, 7)
    when = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0))
    if weekday and weekday.title() != when.strftime("%a"):
        raise ValueError(f"--question-date {value!r}: {when:%Y-%m-%d} is a {when:%a}, not {weekday}")
    return when.strftime("%Y-%m-%d %H:%M" if hour else "%Y-%m-%d")


def mirror_build_message(question: str, passages: list[dict], ref: str, question_date: str | None = None) -> str:
    question = question.strip()
    if not question:
        raise ValueError("the question is empty")
    lines = [f"[ref:{ref}] Question: {question}"]
    if question_date and " " in question_date:
        day, hhmm = question_date.split(" ")
        weekday = datetime.strptime(day, "%Y-%m-%d").strftime("%a")
        lines.append(f"Question date: {day} ({weekday}) {hhmm}, no timezone. Answer as of that moment: treat it as now "
                     "for this question; the clock you see shows the real date and time -- ignore it here.")
    elif question_date:
        lines.append(f"Question date: {question_date}. Answer as of that date: treat {question_date} as today for "
                     "this question; the clock you see shows the real date -- ignore it here.")
    if passages:
        lines += ["", "Evidence:"]
        for n, passage in enumerate(passages, 1):
            about = [f"source: {passage['id']}"] if passage["id"] else []
            about += [f"date: {passage['date']}"] if passage.get("date") else []
            source = f" ({', '.join(about)})" if about else ""
            lines.append(f"[E{n}]{source} {passage['text']}")
        lines += ["", "Answer from the evidence above and cite each passage you use by its label, e.g. [E1]."]
    lines += ["", "When you have your answer, write it on a line of its own that starts exactly with:",
              f"# FINAL ANSWER [ref:{ref}]: ",
              f"If the information you have doesn't answer the question, that line is: # FINAL ANSWER [ref:{ref}]: "
              f"{INSUFFICIENT}",
              "Anything you send before that line is an interim note, not the answer."]
    return "\n".join(lines)


def normalize_passages(evidence: list[dict]) -> list[dict]:
    out = []
    for p in evidence:
        q = {"id": None if p.get("id") is None else str(p["id"]), "text": str(p["text"]).strip()}
        if p.get("date") and str(p["date"]).strip():
            q["date"] = str(p["date"]).strip()
        out.append(q)
    return out


def build(question: str, evidence: list[dict], question_date: str | None) -> tuple[str, str]:
    """(message, "official" | "mirror") for this question, evidence and dataset-format question date."""
    mod = official()
    passages = normalize_passages(evidence)
    if mod is not None:
        qd = mod.parse_question_date(question_date) if question_date else None
        return mod.build_message(question, passages, REF, qd), "official"
    qd = mirror_parse_question_date(question_date) if question_date else None
    return mirror_build_message(question, passages, REF, qd), "mirror"


def message_length(question: str, evidence: list[dict], question_date: str | None) -> tuple[int, str]:
    text, source = build(question, evidence, question_date)
    return len(text), source
