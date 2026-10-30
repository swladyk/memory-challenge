"""Dates: parsing the dataset's format, and conservative resolution of relative expressions.

Relative expressions are resolved against the date of the session that contains them, in that history. Only
unambiguous patterns are resolved; everything else is left as written. Each resolution says what kind of date it is:
the mention itself, a past event, a planned (future) event, or a range.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

DATASET_FMT = "%Y/%m/%d (%a) %H:%M"
_DATASET = re.compile(r"^(\d{4})/(\d{2})/(\d{2}) \(([A-Za-z]{3})\) (\d{2}):(\d{2})$")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
_MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)} | {"sept": 9}
NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a couple of": 2, "couple of": 2}


def parse_dataset_date(value: str) -> datetime | None:
    """``2023/05/20 (Sat) 02:21`` → datetime, or None when malformed or the weekday doesn't match the date."""
    m = _DATASET.match(value or "")
    if not m:
        return None
    try:
        d = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[5]), int(m[6]))
    except ValueError:
        return None
    return d if d.strftime("%a").lower() == m[4].lower() else None


@dataclass(frozen=True)
class Resolution:
    surface: str  # the words as written
    start: date
    end: date  # same as start for a single day
    kind: str  # "mention" (the session day), "past", "planned", "range"
    exact: bool  # False for week/month ranges and year-inferred dates

    def render(self) -> str:
        when = self.start.strftime("%a %Y-%m-%d") if self.start == self.end else \
            f"{self.start:%Y-%m-%d}..{self.end:%Y-%m-%d}"
        label = {"mention": "", "past": "", "planned": ", planned", "range": ""}[self.kind]
        return f"'{self.surface}' = {when}{label}{'' if self.exact else ' (approx.)'}"


_NUM = r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a couple of|couple of)"
_DAYS = "|".join(WEEKDAYS)
_MONTHS_RX = "|".join(MONTHS) + "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
PATTERNS = [
    ("today", re.compile(r"\b(today|tonight|this (?:morning|afternoon|evening))\b", re.I)),
    ("yesterday", re.compile(r"\byesterday\b", re.I)),
    ("tomorrow", re.compile(r"\btomorrow\b", re.I)),
    ("last_weekday", re.compile(rf"\blast ({_DAYS})\b", re.I)),
    ("next_weekday", re.compile(rf"\bnext ({_DAYS})\b", re.I)),
    ("ago", re.compile(rf"\b{_NUM} (day|week|month)s? ago\b", re.I)),
    ("in_n", re.compile(rf"\bin {_NUM} (day|week)s?\b", re.I)),
    ("week_rel", re.compile(r"\b(last|this|next) (week|weekend|month)\b", re.I)),
    ("month_day", re.compile(rf"\b({_MONTHS_RX})\.? (\d{{1,2}})(?:st|nd|rd|th)?(?:,? (\d{{4}}))?\b", re.I)),
]


def _num(s: str) -> int:
    s = s.lower()
    return int(s) if s.isdigit() else NUMBER_WORDS.get(s, 0)


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _month_range(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def resolve_relative(text: str, anchor: datetime | date | None) -> list[Resolution]:
    """Unambiguous relative-date expressions in ``text``, resolved against ``anchor`` (the session date)."""
    if anchor is None:
        return []
    a = anchor.date() if isinstance(anchor, datetime) else anchor
    out: list[Resolution] = []
    seen: set[tuple[int, int]] = set()
    for name, rx in PATTERNS:
        for m in rx.finditer(text):
            if any(s <= m.start() < e for s, e in seen):
                continue
            r = _resolve(name, m, a)
            if r:
                out.append(r)
                seen.add((m.start(), m.end()))
    return out


def _resolve(name: str, m: re.Match, a: date) -> Resolution | None:
    surface = m.group(0)
    if name == "today":
        return Resolution(surface, a, a, "mention", True)
    if name == "yesterday":
        d = a - timedelta(days=1)
        return Resolution(surface, d, d, "past", True)
    if name == "tomorrow":
        d = a + timedelta(days=1)
        return Resolution(surface, d, d, "planned", True)
    if name == "last_weekday":
        target = WEEKDAYS.index(m.group(1).lower())
        back = (a.weekday() - target) % 7 or 7
        d = a - timedelta(days=back)
        return Resolution(surface, d, d, "past", True)
    if name == "next_weekday":
        # "next Friday" is ambiguous between the coming one and the one after; resolve to the coming one, marked approx.
        target = WEEKDAYS.index(m.group(1).lower())
        ahead = (target - a.weekday()) % 7 or 7
        d = a + timedelta(days=ahead)
        return Resolution(surface, d, d, "planned", False)
    if name == "ago":
        n, unit = _num(m.group(1)), m.group(2).lower()
        if not n:
            return None
        days = {"day": 1, "week": 7, "month": 30}[unit] * n
        d = a - timedelta(days=days)
        return Resolution(surface, d, d, "past", unit == "day")
    if name == "in_n":
        n, unit = _num(m.group(1)), m.group(2).lower()
        if not n:
            return None
        d = a + timedelta(days=n * (7 if unit == "week" else 1))
        return Resolution(surface, d, d, "planned", unit == "day")
    if name == "week_rel":
        which, unit = m.group(1).lower(), m.group(2).lower()
        shift = {"last": -1, "this": 0, "next": 1}[which]
        if unit == "month":
            y, mo = a.year, a.month + shift
            y, mo = (y - 1, 12) if mo == 0 else (y + 1, 1) if mo == 13 else (y, mo)
            s, e = _month_range(y, mo)
        else:
            ws = _week_start(a) + timedelta(weeks=shift)
            s, e = (ws + timedelta(days=5), ws + timedelta(days=6)) if unit == "weekend" else (ws, ws + timedelta(days=6))
        kind = "past" if shift < 0 else "planned" if shift > 0 else "range"
        return Resolution(surface, s, e, kind, False)
    if name == "month_day":
        mon = m.group(1).lower()
        month = MONTHS.index(mon) + 1 if mon in MONTHS else _MONTH_ABBR.get(mon[:4] if mon == "sept" else mon[:3])
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else a.year
        try:
            d = date(year, month, day)
        except (TypeError, ValueError):
            return None
        exact = bool(m.group(3))
        if not m.group(3) and (d - a).days > 183:  # "December 28th" said in January means last December
            d = date(year - 1, month, day)
        kind = "past" if d < a else "mention" if d == a else "planned"
        return Resolution(surface, d, d, kind, exact)
    return None


def question_time_range(question: str, question_dt: datetime | None) -> tuple[date, date, str] | None:
    """A date range the question restricts itself to ("in December", "last week", "the past two weeks"), if any."""
    if question_dt is None:
        return None
    q = question.lower()
    a = question_dt.date()
    m = re.search(rf"\b(?:past|last) {_NUM} (day|week|month)s?\b", q)
    if m and _num(m.group(1)):
        days = {"day": 1, "week": 7, "month": 30}[m.group(2)] * _num(m.group(1))
        return a - timedelta(days=days), a, m.group(0)
    m = re.search(r"\b(last|this) (week|month|year)\b", q)
    if m:
        unit, shift = m.group(2), -1 if m.group(1) == "last" else 0
        if unit == "week":
            ws = _week_start(a) + timedelta(weeks=shift)
            return ws, ws + timedelta(days=6), m.group(0)
        if unit == "month":
            y, mo = (a.year, a.month - 1) if shift else (a.year, a.month)
            y, mo = (y - 1, 12) if mo == 0 else (y, mo)
            s, e = _month_range(y, mo)
            return s, e, m.group(0)
        y = a.year + shift
        return date(y, 1, 1), date(y, 12, 31), m.group(0)
    m = re.search(rf"\bin ({'|'.join(MONTHS)})\b", q)
    if m:
        month = MONTHS.index(m.group(1)) + 1
        y = a.year if month <= a.month else a.year - 1
        s, e = _month_range(y, month)
        return s, e, m.group(0)
    return None
