"""BM25 over windows, with an inverted index in plain Python (histories are at most ~10k windows)."""
from __future__ import annotations

import math
from collections import Counter


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.lengths = [len(d) for d in docs]
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 1.0
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, d in enumerate(docs):
            for term, tf in Counter(d).items():
                self.postings.setdefault(term, []).append((i, tf))
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}
        self._norm = [k1 * (1 - b + b * dl / (self.avgdl or 1.0)) for dl in self.lengths]

    def scores(self, query: list[str]) -> dict[int, float]:
        """Score per matching document (documents with no query term are absent)."""
        out: dict[int, float] = {}
        k1 = self.k1
        for term in dict.fromkeys(query):
            plist = self.postings.get(term)
            if not plist:
                continue
            idf = self.idf[term]
            for i, tf in plist:
                out[i] = out.get(i, 0.0) + idf * tf * (k1 + 1) / (tf + self._norm[i])
        return out

    def search(self, query: list[str], k: int) -> list[tuple[int, float]]:
        s = self.scores(query)
        return sorted(s.items(), key=lambda kv: (-kv[1], kv[0]))[:k]

    def doc_freq(self, term: str) -> int:
        return len(self.postings.get(term, ()))
