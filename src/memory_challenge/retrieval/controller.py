"""The bounded retrieval controller: one pass of plan → retrieve → fuse → expand → check → (one recovery) → pack.

No loops beyond the single recovery round and no model calls before Reactor unless dense or rerank are enabled.
Each stage is timed; the result keeps candidate ranks per source, graph paths and the coverage estimate, which the
"why retrieved" diagnostic prints.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from ..config import Config
from ..graph.pagerank import nearest_seed, personalized_pagerank
from ..graph.traversal import expand
from ..indexes.history_index import HistoryIndex
from ..schemas import Candidate, Question
from .coverage import component_coverage
from .fusion import rrf
from .packing import PackResult, pack
from .planner import QueryPlan, make_plan


@dataclass
class RetrievalResult:
    plan: QueryPlan
    candidates: list[Candidate]
    coverage: list[dict]
    pack: PackResult
    timings: dict[str, float] = field(default_factory=dict)
    mode: str = ""
    notes: list[str] = field(default_factory=list)

    def ranked_sessions(self) -> list[str]:
        return list(dict.fromkeys(c.session_id for c in self.candidates))


def _ranked(index: HistoryIndex, hits: list[tuple[int, float]]) -> list[tuple[str, str, float]]:
    return [(index.windows[i].window_id, index.windows[i].session_id, s) for i, s in hits]


class Retriever:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._dense = None
        self._reranker = None

    @property
    def dense(self):
        if self._dense is None:
            from ..indexes.dense import DenseIndex
            self._dense = DenseIndex(self.cfg)
        return self._dense

    @property
    def reranker(self):
        if self._reranker is None:
            from .rerank import Reranker
            self._reranker = Reranker(self.cfg)
        return self._reranker

    def run(self, q: Question, index: HistoryIndex) -> RetrievalResult:
        if q.history_id != index.history.history_id:
            raise ValueError(f"{q.question_id} belongs to {q.history_id}, not {index.history.history_id}")
        if not q.question.strip():
            raise ValueError(f"{q.question_id}: the question is empty")
        if self.cfg.retriever == "kit":
            return kit_retrieve(q, index, self.cfg)
        cfg, t = self.cfg, {}
        t0 = time.perf_counter()
        plan = make_plan(q, cfg)
        t["plan"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        sources = {"lexical": _ranked(index, index.bm25.search(plan.tokens + plan.expansion, cfg.lexical.top_k))}
        if cfg.lexical.per_component and len(plan.components) > 1:
            comp = []
            for c in plan.components:
                comp.append(_ranked(index, index.bm25.search(c.tokens + plan.expansion, cfg.lexical.top_k // 2)))
            sources["component"] = [x for pair in zip(*comp) for x in pair] if comp else []
        t["lexical"] = time.perf_counter() - t0

        if cfg.dense.enabled:
            t0 = time.perf_counter()
            if not hasattr(index, "_dense_matrix"):
                index._dense_matrix = self.dense.embed_docs([w.index_text for w in index.windows])
            sources["dense"] = _ranked(index, self.dense.search(q.question, index._dense_matrix, cfg.dense.top_k))
            t["dense"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        cands = rrf(sources, cfg.fusion.weights, cfg.fusion.rrf_k)
        t["fusion"] = time.perf_counter() - t0

        if plan.time_range and cfg.controller.time_boost > 0:
            lo, hi, _ = plan.time_range
            for c in cands:
                occ = index.history.occurrence(c.session_id)
                if occ and occ.date and lo <= occ.date.date() <= hi:
                    c.score *= 1 + cfg.controller.time_boost
                    c.via.append(f"time boost: session date in {plan.time_range[2]!r}")
            cands.sort(key=lambda c: (-c.score, c.window_id))

        if cfg.rerank.enabled and cands:
            t0 = time.perf_counter()
            texts = {c.window_id: index.window(c.window_id).index_text for c in cands[: cfg.rerank.max_candidates]}
            cands = self.reranker.rerank(q.question, cands, texts)
            t["rerank"] = time.perf_counter() - t0

        if cfg.graph.enabled and cands:
            t0 = time.perf_counter()
            extra = expand(index.graph, cands, plan, cfg.graph)
            cands = sorted(cands + extra, key=lambda c: (-c.score, c.window_id))
            t["graph"] = time.perf_counter() - t0

        if cfg.ppr.enabled and cands and any(h in plan.hints for h in cfg.ppr.hints):
            t0 = time.perf_counter()
            cands = self._ppr(plan, cands, index)
            t["ppr"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        coverage = component_coverage(plan, cands, index, cfg.controller.coverage_top_n)
        t["coverage"] = time.perf_counter() - t0
        notes = []
        gaps = [c for c in coverage if not c["covered"]]
        if cfg.controller.recovery and gaps:
            t0 = time.perf_counter()
            rec = []
            for g in gaps:
                rec += _ranked(index, index.bm25.search(g["tokens"] + plan.expansion, cfg.controller.recovery_k))
            existing = {c.window_id: c for c in cands}
            merged = rrf({"main": [(c.window_id, c.session_id, c.score) for c in cands], "recovery": rec},
                         {"main": 1.0, "recovery": cfg.fusion.weights.get("recovery", 0.5)}, cfg.fusion.rrf_k)
            for m in merged:  # keep the first round's provenance
                if m.window_id in existing:
                    old = existing[m.window_id]
                    old_via = [v for v in old.via if v not in ("main",)]
                    m.ranks = {**old.ranks, **{k: v for k, v in m.ranks.items() if k == "recovery"}}
                    m.scores = {**old.scores, **{k: v for k, v in m.scores.items() if k == "recovery"}}
                    m.via = old_via + (["recovery"] if "recovery" in m.ranks else [])
                    m.strength = max(old.strength, m.strength)
                else:
                    m.via = ["recovery: " + ", ".join(g["component"] for g in gaps)]
            cands = merged
            notes.append(f"recovery round for {len(gaps)} uncovered component(s)")
            coverage = component_coverage(plan, cands, index, cfg.controller.coverage_top_n)
            t["recovery"] = time.perf_counter() - t0

        t0 = time.perf_counter()
        packed = pack(plan, cands, index, cfg)
        t["pack"] = time.perf_counter() - t0
        return RetrievalResult(plan, cands, coverage, packed, t, cfg.mode(), notes)

    def _ppr(self, plan, cands: list[Candidate], index: HistoryIndex) -> list[Candidate]:
        cfg = self.cfg
        g = index.graph
        seeds = {f"win:{c.window_id}": c.score for c in cands[: cfg.ppr.seed_count]}
        top = cands[0].score if cands else 1.0
        for a in plan.anchors:
            node = f"ent:{a.lower()}"
            if node in g.adj:
                seeds[node] = top
        scores, iters = personalized_pagerank(g, seeds, cfg.ppr)
        ranked = sorted(((n[4:], s) for n, s in scores.items() if n.startswith("win:")), key=lambda x: -x[1])
        ranked = ranked[: cfg.ppr.top_k]
        seed_set = set(seeds)
        src = []
        for wid, s in ranked:
            src.append((wid, index.window(wid).session_id, s))
        existing = {c.window_id: c for c in cands}
        top_ppr = src[0][2] if src else 1.0
        merged = rrf({"main": [(c.window_id, c.session_id, c.score) for c in cands], "ppr": src},
                     {"main": 1.0, "ppr": cfg.fusion.weights.get("ppr", 0.5)}, cfg.fusion.rrf_k)
        for m in merged:
            old = existing.get(m.window_id)
            if old:
                m.ranks = {**old.ranks, **({"ppr": m.ranks["ppr"]} if "ppr" in m.ranks else {})}
                m.scores = {**old.scores, **({"ppr": m.scores["ppr"]} if "ppr" in m.scores else {})}
                m.via = [v for v in old.via] + ([f"ppr ({iters} iterations)"] if "ppr" in m.ranks else [])
                m.strength = old.strength
            else:
                path = nearest_seed(g, f"win:{m.window_id}", seed_set)
                m.via = [f"ppr ({iters} iterations) via {' → '.join(path) if path else 'distant seeds'}"]
                m.strength = 0.5 * m.scores.get("ppr", 0.0) / top_ppr if top_ppr else 0.0
        return merged


KIT_WORD = re.compile(r"\w{4,}")


def kit_retrieve(q: Question, index: HistoryIndex, cfg: Config) -> RetrievalResult:
    """The supplied baseline (reactor/examples/run_benchmark.py:34-46), re-implemented for ablation: the 4 turns sharing
    most 4+ letter words with the question, each cut to 3,000 characters, one passage per turn."""
    from ..reactor.message import message_length
    from .packing import PackResult
    t0 = time.perf_counter()
    plan = make_plan(q, cfg)
    words = set(KIT_WORD.findall(q.question.lower()))
    scored = []
    for occ in index.history.occurrences:
        content = index.content(occ.session_id)
        for turn in content.turns:
            overlap = len(words & set(KIT_WORD.findall(turn.content.lower())))
            if overlap:
                scored.append((overlap, occ, turn))
    scored.sort(key=lambda s: -s[0])
    evidence, cands, manifest = [], [], []
    for n, (ov, occ, turn) in enumerate(scored[:4], 1):
        evidence.append({"id": occ.session_id, "date": occ.date_str, "text": f"{turn.role}: {turn.content}"[: 12000 // 4]})
        cands.append(Candidate(f"{occ.session_id}:{turn.index}:kit", occ.session_id, float(ov), {"kit": n}, {"kit": float(ov)}, ["kit"]))
        manifest.append({"label": f"E{n}", "session_id": occ.session_id, "date": occ.date_str, "windows": [], "via": [["kit"]]})
    chars, builder = message_length(q.question, evidence, q.question_date)
    packed = PackResult(evidence, q.question, chars, 16000, builder, manifest)
    return RetrievalResult(plan, cands, [], packed, {"kit": time.perf_counter() - t0}, "kit-keyword")
