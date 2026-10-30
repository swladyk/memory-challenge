"""Citation fallback for answers Reactor gave without usable ``[E#]`` labels.

The organisers (event channel, 2026-10-30): "Final answer must come from Reactor, but retrieval and evidence
attribution are handled by your code. The provided client extracts [E#] citations from the FINAL ANSWER only, while
the benchmark expects session IDs. If those labels are missing, your code can select supporting sessions from the
retrieved evidence: just make sure they actually support Reactor's answer rather than blindly falling back to the
top-ranked result."

So, for a prediction that answers (``abstain`` false) but cites nothing, this looks only at the passages that were
sent with that question (its payload) and keeps the sessions whose quoted text supports Reactor's answer:

- answer units = numbers (``25:50``, ``2.5``, ``1,200`` → ``1200``) and stemmed content words of the answer;
- a passage supports the answer when at least ``min_support`` of the units occur in its quoted text (notes in
  ``[note: ...]`` lines are ignored) and at least one matched unit is *distinctive*: a number with two or more
  digits or a separator, a word written with a capital letter in the answer, or a word of five or more letters.
  Single digits and short common words never support an answer on their own;
- every supporting passage is cited, best support first, at most ``max_sessions``; a session outside the
  question's own history is never cited.

When nothing qualifies, citations stay empty: there is no fallback to the top-ranked passage. The answer text is never
changed. Every decision is written to a report.
"""
from __future__ import annotations

import re

from ..text import STOP_WORDS, stem

MIN_SUPPORT = 0.6
MAX_SESSIONS = 3
NUMBER = re.compile(r"\d+(?:[.:/]\d+)*")
WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


def _numbers(text: str) -> set[str]:
    return set(NUMBER.findall(re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)))


def _words(text: str) -> set[str]:
    return {stem(w.lower()) for w in WORD.findall(text) if w.lower() not in STOP_WORDS and len(w) > 2}


def answer_units(answer: str) -> list[tuple[str, str, bool]]:
    """(kind, unit, distinctive) for each number and content word of the answer."""
    units: dict[tuple[str, str], bool] = {}
    for n in _numbers(answer):
        units[("number", n)] = len(re.sub(r"\D", "", n)) >= 2 or bool(re.search(r"[.:/]", n))
    for w in WORD.findall(answer):
        lw = w.lower()
        if lw in STOP_WORDS or len(w) <= 2:
            continue
        key = ("word", stem(lw))
        units[key] = units.get(key, False) or w[0].isupper() or len(w) >= 5
    return [(k, u, d) for (k, u), d in units.items()]


def quoted_text(passage_text: str) -> str:
    return "\n".join(l for l in passage_text.split("\n") if not l.startswith("[note:"))


def support(answer: str, passage_text: str) -> dict:
    units = answer_units(answer)
    text = quoted_text(passage_text)
    nums, words = _numbers(text), _words(text)
    matched = [(k, u, d) for k, u, d in units if (u in nums if k == "number" else u in words)]
    share = len(matched) / len(units) if units else 0.0
    return {"share": round(share, 3), "units": len(units), "matched": [u for _, u, _ in matched],
            "distinctive": [u for _, u, d in matched if d]}


def choose(answer: str, payload: dict, own_sessions: set[str], min_support: float = MIN_SUPPORT,
           max_sessions: int = MAX_SESSIONS) -> tuple[list[str], dict]:
    """(session ids to cite, decision record) for one answered question with no citations."""
    scored, rejected = [], []
    for e in payload.get("evidence", []):
        sid = e.get("id")
        if sid not in own_sessions:
            rejected.append({"session": sid, "reason": "not in this question's history"})
            continue
        s = support(answer, e.get("text", ""))
        if s["share"] >= min_support and s["distinctive"]:
            scored.append((s["share"], sid, s))
        else:
            rejected.append({"session": sid, "reason": "insufficient support", **s})
    scored.sort(key=lambda x: -x[0])
    chosen = list(dict.fromkeys(sid for _, sid, _ in scored))[:max_sessions]
    record = {"filled": chosen, "support": {sid: s for _, sid, s in scored if sid in chosen},
              "rejected": rejected[:20], "min_support": min_support, "max_sessions": max_sessions,
              "reason": "supported by the passage text" if chosen else "no passage supports the answer: left empty"}
    return chosen, record
