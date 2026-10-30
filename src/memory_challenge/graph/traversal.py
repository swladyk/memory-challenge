"""Bounded, typed expansion from seed windows over the history graph.

Only the configured edge types are followed, to ``max_depth`` edges, adding at most ``max_expanded`` windows. Entity
hops go only through entities the question names (when ``anchor_entities_only``) and never through generic entities
(present in more than ``entity_max_session_frac`` of sessions). List hops are taken only for questions that ask for an
item by position, and the item at that position gets the parent's full score instead of a decayed one. Every expanded
candidate records its path.
"""
from __future__ import annotations

from ..config import GraphConfig
from ..schemas import Candidate
from .builder import Graph

EDGE_GROUPS = {"NEXT": {"NEXT"}, "PREV": {"PREV"}, "LIST_ITEM_AT": {"LIST_ITEM_AT", "IN_LIST"},
               "MENTIONS": {"MENTIONS", "MENTIONED_IN"}, "IN_SESSION": {"IN_SESSION", "CONTAINS"}}


def _anchor_match(entity_node: str, anchors: list[str]) -> bool:
    name = entity_node[4:]
    return any(a.lower() in name or name in a.lower() for a in anchors if len(a) > 2)


def expand(graph: Graph, seeds: list[Candidate], plan, cfg: GraphConfig) -> list[Candidate]:
    allowed = set().union(*(EDGE_GROUPS.get(e, {e}) for e in cfg.edge_types))
    if plan.ordinal is None:
        allowed -= EDGE_GROUPS["LIST_ITEM_AT"]
    known = {c.window_id for c in seeds}
    added: dict[str, Candidate] = {}
    frontier = [(f"win:{c.window_id}", c.score, c.strength, [c.window_id]) for c in seeds[: cfg.seed_count]]
    for depth in range(1, cfg.max_depth + 1):
        nxt = []
        for node, score, strength, path in frontier:
            for nb, etype, attrs in graph.adj.get(node, ()):
                if etype not in allowed:
                    continue
                if nb.startswith("ent:"):
                    if graph.generic(nb, cfg.entity_max_session_frac):
                        continue
                    if cfg.anchor_entities_only and not _anchor_match(nb, plan.anchors):
                        continue
                if nb.startswith("occ:"):
                    continue  # session nodes are provenance, not a route between windows
                step = f"{etype}{'#' + str(attrs['ordinal']) if 'ordinal' in attrs else ''}"
                if nb.startswith("win:"):
                    wid = nb[4:]
                    factor = cfg.decay ** depth
                    if etype == "LIST_ITEM_AT" and _ordinal_hit(attrs.get("ordinal"), plan.ordinal, graph, node):
                        factor = 1.0  # the item the question asks for by position keeps its parent's full score
                    if wid in known:
                        continue
                    c = added.get(wid)
                    if c is None:
                        if len(added) >= cfg.max_expanded:
                            continue
                        c = added[wid] = Candidate(wid, wid.rsplit(":", 2)[0], 0.0)
                    if score * factor > c.score:
                        c.score = score * factor
                        c.strength = strength * factor
                        c.scores["graph"] = c.score
                        c.via = [f"graph: {' → '.join(path)} -{step}-> {wid}"]
                nxt.append((nb, score, strength, path + [f"{step}:{nb}"]))
        frontier = nxt
    return sorted(added.values(), key=lambda c: -c.score)


def _ordinal_hit(ordinal, wanted: int, graph: Graph, list_node: str) -> bool:
    if ordinal is None:
        return False
    if wanted == -1:
        siblings = [a.get("ordinal") for _, et, a in graph.adj.get(list_node, ()) if et == "LIST_ITEM_AT"]
        return ordinal == max((o for o in siblings if o is not None), default=None)
    return ordinal == wanted
