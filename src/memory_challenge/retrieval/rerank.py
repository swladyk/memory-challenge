"""Optional cross-encoder reranking of the top candidates.

Scores order candidates; they are not answerability probabilities. A window longer than ``max_chars`` is scored on
the part around its best-matching lines, and that cut is recorded in the candidate's provenance.
"""
from __future__ import annotations

from ..config import Config
from ..optional import require
from ..schemas import Candidate
from ..text import tokenize


def _focus(text: str, query_tokens: set[str], max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    lines = text.split("\n")
    best = max(range(len(lines)), key=lambda i: len(query_tokens & set(tokenize(lines[i]))))
    lo = hi = best
    size = len(lines[best])
    while size < max_chars and (lo > 0 or hi < len(lines) - 1):
        if hi < len(lines) - 1:
            hi += 1
            size += len(lines[hi]) + 1
        if lo > 0 and size < max_chars:
            lo -= 1
            size += len(lines[lo]) + 1
    return "\n".join(lines[lo:hi + 1])[:max_chars], True


class Reranker:
    def __init__(self, cfg: Config):
        self.cfg = cfg.rerank
        st = require("sentence_transformers", "Cross-encoder reranking")
        self.model = st.CrossEncoder(self.cfg.model_id, revision=self.cfg.revision, device=self.cfg.device)

    def rerank(self, question: str, candidates: list[Candidate], texts: dict[str, str]) -> list[Candidate]:
        head = candidates[: self.cfg.max_candidates]
        qt = set(tokenize(question))
        pairs, cut = [], {}
        for c in head:
            t, was_cut = _focus(texts[c.window_id], qt, self.cfg.max_chars)
            pairs.append((question, t))
            cut[c.window_id] = was_cut
        scores = self.model.predict(pairs, batch_size=self.cfg.batch_size, show_progress_bar=False)
        for c, s in zip(head, scores):
            c.scores["rerank"] = float(s)
            c.via.append("rerank" + (" (scored on a focused cut)" if cut[c.window_id] else ""))
        ordered = sorted(head, key=lambda c: -c.scores["rerank"])
        for r, c in enumerate(ordered, 1):
            c.ranks["rerank"] = r
        return ordered + candidates[self.cfg.max_candidates:]
