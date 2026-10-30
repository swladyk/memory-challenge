"""Everything retrieval needs about one history: its windows, BM25 over them, and lazily its graph and embeddings.

An index is built for exactly one history; windows of other histories never enter it. Window construction is
memoised per (session content hash, window config version), so sessions reused across histories are windowed once
per process, while dates come from this history's occurrences.
"""
from __future__ import annotations

import time

from ..config import Config
from ..data.windows import build_windows
from ..schemas import History, SessionContent, Window
from ..text import tokenize
from .lexical import BM25

_WINDOW_MEMO: dict[tuple[str, str, str], tuple[list[Window], list[list[str]]]] = {}


def windows_for(content: SessionContent, cfg: Config) -> tuple[list[Window], list[list[str]]]:
    key = (content.session_id, content.content_hash, repr(cfg.window))
    got = _WINDOW_MEMO.get(key)
    if got is None:
        ws = build_windows(content, cfg.window)
        got = (ws, [tokenize(w.index_text) for w in ws])
        if len(_WINDOW_MEMO) > 200_000:
            _WINDOW_MEMO.clear()
        _WINDOW_MEMO[key] = got
    return got


class HistoryIndex:
    def __init__(self, history: History, cfg: Config):
        t0 = time.perf_counter()
        self.history, self.cfg = history, cfg
        self.windows: list[Window] = []
        self.tokens: list[list[str]] = []
        for occ in history.occurrences:
            ws, toks = windows_for(history.contents[occ.session_id], cfg)
            self.windows += ws
            self.tokens += toks
        self.by_id = {w.window_id: i for i, w in enumerate(self.windows)}
        self.session_windows: dict[str, list[int]] = {}
        for i, w in enumerate(self.windows):
            self.session_windows.setdefault(w.session_id, []).append(i)
        self.bm25 = BM25(self.tokens, cfg.lexical.k1, cfg.lexical.b)
        self._graph = None
        self.build_seconds = time.perf_counter() - t0

    def window(self, window_id: str) -> Window:
        return self.windows[self.by_id[window_id]]

    def content(self, session_id: str) -> SessionContent:
        return self.history.contents[session_id]

    @property
    def graph(self):
        if self._graph is None:
            from ..graph.builder import build_graph
            self._graph = build_graph(self)
        return self._graph
