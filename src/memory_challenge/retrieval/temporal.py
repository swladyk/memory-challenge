"""Derived temporal metadata for packed evidence: clearly marked notes, never quotes, never answers.

Per passage: how many days before the question the session is dated (in this history), and unambiguous relative
expressions resolved against that date. Per question (temporal plans only): a small table of grounded dates with
their sources and the day differences between them, each with inputs, unit, operation and result. Dates are kept
per mention; a later mention of a different event never replaces an earlier one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from itertools import combinations

from ..config import TemporalConfig
from ..data.dates import Resolution, resolve_relative


@dataclass(frozen=True)
class DatedItem:
    label: str  # E-label of the passage
    session_id: str
    when: date
    what: str  # "session date" or the resolved expression
    kind: str  # "mention" | "past" | "planned" | "range"
    exact: bool


def passage_notes(text: str, session_dt: datetime | None, question_dt: datetime | None, cfg: TemporalConfig
                  ) -> tuple[list[str], list[Resolution]]:
    notes: list[str] = []
    res: list[Resolution] = []
    if session_dt and question_dt and cfg.days_before_note:
        days = (question_dt.date() - session_dt.date()).days
        notes.append(f"session dated {days} day{'s' if days != 1 else ''} before the question")
    if session_dt and cfg.resolve_relative:
        res = resolve_relative(text, session_dt)
        if res:
            notes.append("; ".join(r.render() for r in res[:6]))
    return notes, res


def derived_dates(items: list[DatedItem], cfg: TemporalConfig) -> list[str]:
    """Lines listing grounded dates and pairwise day differences (inputs, unit, sources, operation, result)."""
    if not items or not cfg.derived_calculations:
        return []
    items = items[: cfg.max_derived_dates]
    lines = ["Dates found in the evidence (computed from session dates; not quotes):"]
    for it in items:
        tag = "" if it.exact else " (approx.)"
        lines.append(f"- {it.label} ({it.session_id}): {it.what} = {it.when:%a %Y-%m-%d}{', planned' if it.kind == 'planned' else ''}{tag}")
    pairs = []
    for a, b in combinations(items, 2):
        if a.label == b.label and a.what == b.what:
            continue
        days = abs((b.when - a.when).days)
        pairs.append(f"- days between {a.label} {a.what} ({a.when:%Y-%m-%d}) and {b.label} {b.what} ({b.when:%Y-%m-%d}) = {days}")
    if pairs:
        lines.append("Day differences (end date minus start date, in days; count +1 to include both days):")
        lines += pairs[:10]
    return lines
