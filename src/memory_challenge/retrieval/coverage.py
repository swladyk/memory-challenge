"""Heuristic coverage of a plan's components by the top candidates.

A component counts as covered when some top window contains at least half of its content words. This is a lexical
estimate of whether retrieval has touched each part of the question; it does not show that the answer is present.
"""
from __future__ import annotations

from ..schemas import Candidate

LABEL = "heuristic lexical coverage (not proof of answerability)"


def component_coverage(plan, candidates: list[Candidate], index, top_n: int) -> list[dict]:
    out = []
    head = candidates[:top_n]
    for comp in plan.components:
        need = set(comp.tokens)
        if not need:
            continue
        hits = []
        for c in head:
            toks = set(index.tokens[index.by_id[c.window_id]])
            share = len(need & toks) / len(need)
            if share >= 0.5:
                hits.append(c.window_id)
        out.append({"component": comp.label, "tokens": comp.tokens, "covered": bool(hits), "windows": hits[:5],
                    "method": LABEL})
    return out
