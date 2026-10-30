"""A history-local provenance and structure graph.

Nodes: ``occ:<session>`` (a session occurrence in this history), ``win:<window>``, ``list:<session>:<list>``,
``ent:<name>``. Edges, by origin:

- structural (deterministic, exact): IN_SESSION / CONTAINS (window ↔ occurrence), NEXT / PREV (consecutive windows of
  a session), LIST_ITEM_AT / IN_LIST (list ↔ its item windows, with the item's ordinal);
- rule-extracted (deterministic but heuristic): MENTIONS / MENTIONED_IN (window ↔ capitalised name or quoted title).

There are no model-extracted or similarity edges in this graph. It is built from one history's windows only, so no
path can pass through another user's sessions even when session contents are shared across histories.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..schemas import Window
from ..text import BOILERPLATE, STOP_WORDS

STRUCTURAL = {"IN_SESSION", "CONTAINS", "NEXT", "PREV", "LIST_ITEM_AT", "IN_LIST"}
RULE_EXTRACTED = {"MENTIONS", "MENTIONED_IN"}
NAME = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z][a-zA-Z'&-]+(?:\s+(?:of |the |de |la )?[A-Z][a-zA-Z'&-]+)*)")
QUOTED = re.compile(r"[\"“]([^\"”\n]{3,60})[\"”]")
NOT_NAMES = STOP_WORDS | BOILERPLATE | {
    "here", "however", "also", "additionally", "finally", "first", "second", "third", "next", "then", "overall",
    "remember", "consider", "make", "try", "use", "keep", "good", "great", "sure", "yes", "thanks", "thank", "hello",
    "hi", "please", "note", "tip", "tips", "step", "example", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "january", "february", "march", "april", "june", "july", "august", "september", "october",
    "november", "december", "ai", "i'm", "i've", "i'd", "i'll"}


def extract_entities(text: str) -> set[str]:
    """Capitalised names (not sentence-initial) and quoted titles, lowercased. Heuristic; no model involved."""
    out = set()
    for m in NAME.finditer(text):
        name = m.group(1).strip()
        if name.lower() not in NOT_NAMES and len(name) > 2:
            out.add(name.lower())
    for m in QUOTED.finditer(text):
        out.add(m.group(1).strip().lower())
    return out


@dataclass
class Graph:
    adj: dict[str, list[tuple[str, str, dict]]] = field(default_factory=dict)
    entity_sessions: dict[str, set[str]] = field(default_factory=dict)
    n_sessions: int = 0

    def add(self, u: str, v: str, etype: str, back: str, **attrs) -> None:
        self.adj.setdefault(u, []).append((v, etype, attrs))
        self.adj.setdefault(v, []).append((u, back, attrs))

    def generic(self, entity_node: str, max_frac: float) -> bool:
        s = self.entity_sessions.get(entity_node, ())
        return self.n_sessions > 0 and len(s) / self.n_sessions > max_frac

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for u, edges in self.adj.items():
            for _, et, _ in edges:
                counts[et] = counts.get(et, 0) + 1
        kinds: dict[str, int] = {}
        for u in self.adj:
            kinds[u.split(":", 1)[0]] = kinds.get(u.split(":", 1)[0], 0) + 1
        return {"nodes": kinds, "edges": counts}


def _main_text(index, w: Window) -> str:
    content = index.content(w.session_id)
    return "\n".join(content.turns[s.turn_index].content[s.start:s.end] for s in w.spans if not s.context)


def build_graph(index) -> Graph:
    g = Graph(n_sessions=len(index.history.occurrences))
    for occ in index.history.occurrences:
        ids = index.session_windows.get(occ.session_id, [])
        prev = None
        for i in ids:
            w = index.windows[i]
            wn = f"win:{w.window_id}"
            g.add(wn, f"occ:{occ.session_id}", "IN_SESSION", "CONTAINS")
            if prev is not None:
                g.add(prev, wn, "NEXT", "PREV")
            prev = wn
            for item in w.list_items:
                g.add(f"list:{w.session_id}:{item.list_id}", wn, "LIST_ITEM_AT", "IN_LIST", ordinal=item.ordinal)
            for ent in extract_entities(_main_text(index, w)):
                en = f"ent:{ent}"
                g.add(wn, en, "MENTIONS", "MENTIONED_IN")
                g.entity_sessions.setdefault(en, set()).add(w.session_id)
    return g
