"""Optional dense retrieval: a sentence-transformers bi-encoder with embeddings cached by content.

Exact search (a matrix–vector product) over one history's windows: at ≤ ~10k windows per history an ANN index adds
nothing. Embeddings are cached per (model id, revision, window config version, document prefix) and per window
content hash, so sessions reused across histories are encoded once. Nothing here runs unless ``dense.enabled``.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import Config
from ..optional import require


class DenseIndex:
    def __init__(self, cfg: Config):
        self.cfg = cfg.dense
        self.window_version = cfg.window.version
        st = require("sentence_transformers", "Dense retrieval")
        self.np = require("numpy", "Dense retrieval")
        self.model = st.SentenceTransformer(self.cfg.model_id, revision=self.cfg.revision, device=self.cfg.device)
        key = f"{self.cfg.model_id}@{self.cfg.revision}|{self.window_version}|{self.cfg.doc_prefix}"
        self.cache_dir = Path(self.cfg.cache_dir) / hashlib.sha1(key.encode()).hexdigest()[:16]
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, text: str) -> str:
        return hashlib.sha1(text.encode()).hexdigest()

    def embed_docs(self, texts: list[str]):
        np = self.np
        keys = [self._key(t) for t in texts]
        out = [None] * len(texts)
        todo = []
        for i, k in enumerate(keys):
            p = self.cache_dir / f"{k}.npy"
            if p.exists():
                out[i] = np.load(p)
            else:
                todo.append(i)
        if todo:
            vecs = self.model.encode([self.cfg.doc_prefix + texts[i] for i in todo], batch_size=self.cfg.batch_size,
                                     normalize_embeddings=True, show_progress_bar=False)
            for i, v in zip(todo, vecs):
                np.save(self.cache_dir / f"{keys[i]}.npy", v)
                out[i] = v
        return np.vstack(out) if out else np.zeros((0, 1))

    def search(self, query: str, doc_matrix, k: int) -> list[tuple[int, float]]:
        q = self.model.encode([self.cfg.query_prefix + query], normalize_embeddings=True, show_progress_bar=False)[0]
        sims = doc_matrix @ q
        order = self.np.argsort(-sims)[:k]
        return [(int(i), float(sims[i])) for i in order]
