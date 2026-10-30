"""Personalized PageRank over one history's graph, as an optional extra ranking source.

Inspired by associative retrieval (HippoRAG-style seeding from query matches), but not a reproduction of HippoRAG 2:
there is no OpenIE triple extraction, no synonym edges and no recognition-memory filter. Seeds are the top fused
windows (weighted by fused score) plus entity nodes the question names. Edge weights are per type; entity edges are
down-weighted by the entity's degree so generic hubs don't dominate. Dangling mass returns to the seeds. Graph
centrality is not answer relevance: the output is one fusion source among others, and time filtering stays separate.
"""
from __future__ import annotations

import math

from ..config import PPRConfig
from .builder import Graph

CANONICAL = {"IN_LIST": "LIST_ITEM_AT", "MENTIONED_IN": "MENTIONS", "CONTAINS": "IN_SESSION"}


def personalized_pagerank(graph: Graph, seeds: dict[str, float], cfg: PPRConfig) -> tuple[dict[str, float], int]:
    """(score per node, iterations run). ``seeds``: node → non-negative weight (normalised here)."""
    total = sum(w for w in seeds.values() if w > 0)
    if total <= 0:
        return {}, 0
    s = {n: w / total for n, w in seeds.items() if w > 0 and n in graph.adj}
    if not s:
        return {}, 0
    out_w: dict[str, list[tuple[str, float]]] = {}
    for u, edges in graph.adj.items():
        row = []
        for v, et, _ in edges:
            canon = CANONICAL.get(et, et)
            w = cfg.edge_weights.get(canon, 0.0)
            if canon == "MENTIONS":
                hub = u if u.startswith("ent:") else v
                w = w / math.log(2 + len(graph.adj.get(hub, ())))
            if w > 0:
                row.append((v, w))
        tot = sum(w for _, w in row)
        if tot > 0:
            out_w[u] = [(v, w / tot) for v, w in row]
    r = dict(s)
    d = cfg.damping
    it = 0
    for it in range(1, cfg.max_iter + 1):
        nxt: dict[str, float] = {n: (1 - d) * p for n, p in s.items()}
        dangling = 0.0
        for u, mass in r.items():
            row = out_w.get(u)
            if not row:
                dangling += mass
                continue
            for v, p in row:
                nxt[v] = nxt.get(v, 0.0) + d * mass * p
        if dangling:
            for n, p in s.items():
                nxt[n] = nxt.get(n, 0.0) + d * dangling * p
        diff = sum(abs(nxt.get(n, 0.0) - r.get(n, 0.0)) for n in set(nxt) | set(r))
        r = nxt
        if diff < cfg.tol:
            break
    return r, it


def nearest_seed(graph: Graph, node: str, seeds: set[str], max_hops: int = 3) -> list[str] | None:
    """A shortest path from ``node`` back to any seed, for explaining a PPR result."""
    frontier, seen = [[node]], {node}
    for _ in range(max_hops):
        nxt = []
        for path in frontier:
            for v, et, _ in graph.adj.get(path[-1], ()):
                if v in seen:
                    continue
                p = path + [f"{et}:{v}"]
                if v in seeds:
                    return p
                seen.add(v)
                nxt.append(p)
        frontier = nxt
    return None
