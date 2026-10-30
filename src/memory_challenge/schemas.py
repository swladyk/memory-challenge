"""Typed records shared by every layer.

Content and occurrence are kept apart: ``SessionContent`` is what was said (identical wherever the session id
appears); ``SessionOccurrence`` is where and when it appears in one user's history. Dates, positions and anything
derived from them belong to occurrences only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Turn:
    turn_id: str
    role: str  # "user" | "assistant"
    content: str
    index: int  # position in its session


@dataclass(frozen=True)
class SessionContent:
    session_id: str
    content_hash: str
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class SessionOccurrence:
    history_id: str
    session_id: str
    date_str: str  # exactly as in histories.jsonl
    date: datetime | None  # parsed; None when the string is malformed
    position: int  # chronological index within the history


@dataclass
class History:
    history_id: str
    occurrences: list[SessionOccurrence]
    contents: dict[str, SessionContent]

    def __post_init__(self) -> None:
        self._by_id = {o.session_id: o for o in self.occurrences}

    def occurrence(self, session_id: str) -> SessionOccurrence | None:
        return self._by_id.get(session_id)

    def has_session(self, session_id: str) -> bool:
        return session_id in self._by_id


@dataclass(frozen=True)
class Question:
    question_id: str
    history_id: str
    question: str
    question_date: str


@dataclass(frozen=True)
class Span:
    """A character range of one turn's original content. ``context`` marks text included to interpret the main span
    (the previous assistant turn's tail for a short reply, a list's lead-in line), not text that matched."""
    turn_index: int
    start: int
    end: int
    context: bool = False


@dataclass(frozen=True)
class ListItem:
    list_id: str  # session-unique id of the list (turn index + list number within the turn)
    ordinal: int  # the item's number as written (numbered lists) or its 1-based position (bullets)
    numbered: bool


@dataclass(frozen=True)
class Window:
    """A retrievable unit of original text from one session, with exact source spans."""
    window_id: str
    session_id: str
    role: str  # role of the main (non-context) span
    spans: tuple[Span, ...]
    index_text: str  # what the retrievers see (may include a context prefix)
    list_items: tuple[ListItem, ...] = ()
    kind: str = "turn"  # "turn" | "segment" | "reply"


@dataclass
class Candidate:
    """A window proposed for one question, with where it came from (ranks per source, graph path)."""
    window_id: str
    session_id: str
    score: float  # fused rank score: orders candidates
    ranks: dict[str, int] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    via: list[str] = field(default_factory=list)
    strength: float = 0.0  # best raw score relative to that source's top (0..1): gates packing, unlike rank fusion


@dataclass
class BoundEvidence:
    """A passage bound to one history occurrence: what is sent to Reactor under one ``[E#]`` label."""
    history_id: str
    session_id: str
    date: str  # this history's date for the session
    text: str  # original excerpts with speaker labels, plus clearly marked notes
    window_ids: list[str]
    spans: list[dict]
    notes: list[str]
    score: float
