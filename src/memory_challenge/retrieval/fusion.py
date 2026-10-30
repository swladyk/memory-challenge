"""Reciprocal rank fusion with per-source weights; keeps each candidate's rank in every source."""
from __future__ import annotations

from ..schemas import Candidate


STRENGTH_SOURCES = {"lexical": 1.0, "component": 1.0, "dense": 1.0, "recovery": 0.8}


def rrf(sources: dict[str, list[tuple[str, str, float]]], weights: dict[str, float], k: int) -> list[Candidate]:
    """``sources``: name → ranked [(window_id, session_id, raw score)]. Score = Σ weight / (k + rank).

    Rank fusion flattens scores (rank 1 and rank 100 differ by less than 3×), so each candidate also gets a
    ``strength``: its raw score over the top raw score of the same source, maximised over score-bearing sources."""
    out: dict[str, Candidate] = {}
    for name, ranked in sources.items():
        w = weights.get(name, 1.0)
        if w <= 0:
            continue
        seen: set[str] = set()
        top = max((raw for _, _, raw in ranked), default=0.0)
        for rank, (wid, sid, raw) in enumerate(ranked, 1):
            if wid in seen:
                continue
            seen.add(wid)
            c = out.setdefault(wid, Candidate(wid, sid, 0.0))
            c.score += w / (k + rank)
            c.ranks[name] = rank
            c.scores[name] = raw
            c.via.append(name)
            if name in STRENGTH_SOURCES and top > 0:
                c.strength = max(c.strength, STRENGTH_SOURCES[name] * raw / top)
    return sorted(out.values(), key=lambda c: (-c.score, c.window_id))
