"""Run configuration: dataclasses with documented defaults, loaded from TOML, hashed for manifests.

Every parameter that changes retrieval output is here, so a run's ``config_hash`` identifies its behaviour.
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path


@dataclass(frozen=True)
class WindowConfig:
    max_chars: int = 2000  # longest window of main text; long turns are split into blocks up to this size
    # (2,000 beat 1,200 and 3,000 on a dev-large tune sample: docs/framework_results.md)
    overlap_blocks: int = 1  # blocks repeated at the start of the next segment of a long turn
    short_reply_chars: int = 60  # user turns shorter than this carry the previous assistant turn's tail as context
    context_chars: int = 300  # how much neighbouring text is attached as context
    prompt_prefix_chars: int = 200  # assistant windows are indexed with this much of the preceding user turn
    version: str = "w1"  # bump when windowing changes, to invalidate caches


@dataclass(frozen=True)
class LexicalConfig:
    k1: float = 1.2
    b: float = 0.75
    top_k: int = 200
    drop_boilerplate: bool = True  # drop "remind me / previous chat / mentioned" words from queries
    expand_attributes: bool = False  # add phrasings for attribute questions (age, duration, price, distance)
    per_component: bool = True  # also search each question component separately and fuse


@dataclass(frozen=True)
class DenseConfig:
    enabled: bool = False
    model_id: str = "BAAI/bge-small-en-v1.5"
    revision: str = "main"
    query_prefix: str = "Represent this sentence for searching relevant passages: "
    doc_prefix: str = ""
    batch_size: int = 64
    device: str = "cpu"
    top_k: int = 200
    cache_dir: str = ".cache/embeddings"


@dataclass(frozen=True)
class FusionConfig:
    rrf_k: int = 60
    weights: dict = field(default_factory=lambda: {"lexical": 1.0, "component": 0.5, "dense": 1.0, "ppr": 0.5,
                                                   "recovery": 0.5})


@dataclass(frozen=True)
class RerankConfig:
    enabled: bool = False
    model_id: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    revision: str = "main"
    max_candidates: int = 50
    batch_size: int = 16
    device: str = "cpu"
    max_chars: int = 2000  # longer windows are scored on their best-matching part, and the cut is recorded


@dataclass(frozen=True)
class GraphConfig:
    enabled: bool = False
    edge_types: tuple = ("NEXT", "PREV", "LIST_ITEM_AT", "MENTIONS")
    seed_count: int = 10  # top fused windows that seed expansion
    max_depth: int = 2  # MENTIONS reaches another window in two hops (window → entity → window)
    max_expanded: int = 30
    decay: float = 0.5  # an expanded window scores parent score × decay^depth
    entity_max_session_frac: float = 0.05  # entities in more sessions than this are too generic to expand through
    anchor_entities_only: bool = True  # expand through entities named in the question only


@dataclass(frozen=True)
class PPRConfig:
    enabled: bool = False
    damping: float = 0.85  # probability of following an edge; 1 - damping restarts at the seeds
    max_iter: int = 50
    tol: float = 1e-6
    seed_count: int = 10
    top_k: int = 50
    hints: tuple = ("aggregation", "temporal", "update")  # run only when the plan has one of these hints
    edge_weights: dict = field(default_factory=lambda: {"IN_SESSION": 0.5, "NEXT": 1.0, "PREV": 1.0,
                                                        "LIST_ITEM_AT": 1.0, "MENTIONS": 1.0})


@dataclass(frozen=True)
class ControllerConfig:
    recovery: bool = True  # one extra retrieval round for components the first round did not cover
    recovery_k: int = 30
    coverage_top_n: int = 20  # candidates inspected when estimating component coverage
    time_boost: float = 0.25  # score boost for windows whose session falls in the question's time range


@dataclass(frozen=True)
class PackingConfig:
    max_message_chars: int = 16000  # the client's default; the server limit is 32,000 (may change)
    max_sessions: int = 12
    max_windows_per_session: int = 4
    score_floor: float = 0.4  # windows whose strength (raw score / top raw score of its source) is below this are not packed
    novelty_bonus: float = 0.15  # added to the strength of the first window of a session not yet packed
    chronological: bool = True
    notes: bool = True  # derived, clearly marked notes (dates, days before question)
    guidance: bool = True  # a fixed answering-guidance paragraph appended to the question text
    max_window_chars: int = 2500  # a single excerpt longer than this is trimmed around its matching lines
    min_trim_chars: int = 200  # a window that doesn't fit is cut to the room left only if at least this much is left


@dataclass(frozen=True)
class TemporalConfig:
    resolve_relative: bool = True
    days_before_note: bool = True
    derived_calculations: bool = True
    max_derived_dates: int = 6


@dataclass(frozen=True)
class ReactorConfig:
    client_path: str = "reactor/client.py"
    base_url_env: str = "REACTOR_BASE_URL"  # not secret; read from the environment or passed on the CLI
    agent_ids_env: str = "REACTOR_AGENT_IDS"  # each team has one agent (organisers, 2026-10-30)
    # Latency grows as a history accumulates questions (~60 s at first, up to ~200 s, occasionally over 300 s); the
    # timeout is only how long the client waits, Reactor keeps working (organisers, 2026-10-31: use e.g. 600 s).
    timeout_s: float = 600.0
    retry_refused: int = 3  # extra attempts after a refusal the server says was never forwarded (busy/previous_pending)
    refused_backoff_s: float = 7.0  # organisers: previous_pending right after an answer is expected; retry after 5-10 s
    # A timed-out question still runs on Reactor: its late final answer is salvaged when a later request in the same
    # history reports it. Still unanswered when the history's questions are done: one drain ping, then re-asked this
    # many times in the same Reactor history (organisers, 2026-10-31).
    reask_timed_out: int = 1
    ping_text: str = "Reply only OK."


@dataclass(frozen=True)
class Config:
    name: str = "baseline"
    retriever: str = "windows"  # "windows" (this framework) or "kit" (the supplied keyword baseline, for ablation)
    window: WindowConfig = field(default_factory=WindowConfig)
    lexical: LexicalConfig = field(default_factory=LexicalConfig)
    dense: DenseConfig = field(default_factory=DenseConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    rerank: RerankConfig = field(default_factory=RerankConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    ppr: PPRConfig = field(default_factory=PPRConfig)
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    packing: PackingConfig = field(default_factory=PackingConfig)
    temporal: TemporalConfig = field(default_factory=TemporalConfig)
    reactor: ReactorConfig = field(default_factory=ReactorConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    def hash(self) -> str:
        """Hash of everything except the name and Reactor connection settings, which don't change retrieval."""
        d = self.to_dict()
        d.pop("name")
        d.pop("reactor")
        return hashlib.sha256(json.dumps(d, sort_keys=True, default=list).encode()).hexdigest()[:16]

    def mode(self) -> str:
        """Which retrieval components actually run, for manifests and reports."""
        if self.retriever == "kit":
            return "kit-keyword"
        parts = ["lexical"]
        if self.dense.enabled:
            parts.append("dense")
        if self.rerank.enabled:
            parts.append("rerank")
        if self.graph.enabled:
            parts.append("graph")
        if self.ppr.enabled:
            parts.append("ppr")
        if self.controller.recovery:
            parts.append("recovery")
        return "+".join(parts)


def _build(cls, data: dict):
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"{cls.__name__}: unknown setting(s) {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        default = getattr(cls(), name)
        if is_dataclass(default):
            kwargs[name] = _build(type(default), value)
        elif isinstance(default, tuple):
            kwargs[name] = tuple(value)
        elif isinstance(default, dict):
            kwargs[name] = {**default, **value}
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load_config(path: str | Path | None) -> Config:
    """A Config from a TOML file (sections mirror the dataclasses); defaults when ``path`` is None."""
    if path is None:
        return Config()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    data.setdefault("name", Path(path).stem)
    return _build(Config, data)
