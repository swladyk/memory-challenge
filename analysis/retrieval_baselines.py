"""Retrieval-only baselines on the dev splits: session recall@k and evidence coverage under the message budget.

Exploration only: no Reactor calls, no network, no repository code imported. Ranking functions never see gold;
gold is joined afterwards, in score(), to measure. Each dataset history is indexed on its own (no cross-history state).

Methods
- kit: the participant kit's keyword baseline, re-implemented from reactor/examples/run_benchmark.py:34-46
  (turn level, overlap of 4+ letter words, top 4 turns, each cut to 3,000 characters).
- bm25-<unit>: BM25 (k1=1.2, b=0.75) over Porter-stemmed, stop-word-filtered tokens; unit = turn, round (a user turn
  and the assistant turns that follow it), session, or user (user turns only). Sessions are ranked by their best unit.

    python -I analysis/retrieval_baselines.py --split dev --workers 8
"""
from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy import sparse
from nltk.stem import PorterStemmer
from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

K1, B = 1.2, 0.75
KS = (1, 3, 5, 10, 20, 50)
BUDGETS = (14000, 30000)  # passage-text characters: client default 16,000 and server 32,000, minus template overhead
PASSAGE_HEADER = 61  # characters client.py adds per passage ("[E12] (source: s_..., date: ...) "), measured
UNIT_CAP = 3000  # characters per passage when packing
TOKEN = re.compile(r"[a-z0-9]+")
STOP = frozenset(ENGLISH_STOP_WORDS)
_stemmer = PorterStemmer()


@lru_cache(maxsize=500_000)
def stem(w: str) -> str:
    return _stemmer.stem(w)


def tokens(text: str) -> list[str]:
    return [stem(w) for w in TOKEN.findall(text.lower()) if w not in STOP and len(w) > 1]


def norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+(?:[.:/][0-9]+)*", text.lower()))


class BM25:
    def __init__(self, docs: list[list[str]]):
        vocab: dict[str, int] = {}
        rows, cols, vals = [], [], []
        for i, d in enumerate(docs):
            counts: dict[int, int] = {}
            for t in d:
                j = vocab.setdefault(t, len(vocab))
                counts[j] = counts.get(j, 0) + 1
            rows += [i] * len(counts)
            cols += counts.keys()
            vals += counts.values()
        self.vocab = vocab
        self.n = len(docs)
        self.X = sparse.csc_matrix((vals, (rows, cols)), shape=(len(docs), max(len(vocab), 1)), dtype=np.float32)
        dl = np.asarray([len(d) for d in docs], dtype=np.float32)
        self.norm = K1 * (1 - B + B * dl / max(dl.mean(), 1e-9))
        df = np.diff(self.X.indptr)
        self.idf = np.log(1 + (self.n - df + 0.5) / (df + 0.5)).astype(np.float32)

    def scores(self, query: list[str]) -> np.ndarray:
        cols = sorted({self.vocab[t] for t in query if t in self.vocab})
        if not cols:
            return np.zeros(self.n, dtype=np.float32)
        tf = self.X[:, cols].toarray()
        return (self.idf[cols] * tf * (K1 + 1) / (tf + self.norm[:, None])).sum(axis=1)

    def max_score(self, query: list[str]) -> float:
        """Upper bound of a single unit's score for this query (every term at saturation): for normalisation."""
        cols = sorted({self.vocab[t] for t in query if t in self.vocab})
        return float(self.idf[cols].sum() * (K1 + 1)) if cols else 0.0


def build_units(history: dict) -> dict:
    """Unit texts, their session ids and token lists, per granularity. Tokenises each turn once."""
    turn_rows = []  # (session_index, role, content, tokens)
    for si, s in enumerate(history["sessions"]):
        for t in s["turns"]:
            turn_rows.append((si, t["role"], t["content"], tokens(t["content"])))
    units = {"turn": [], "user": [], "round": [], "session": []}
    cur = None
    for si, role, content, toks in turn_rows:
        text = f"{role}: {content}"
        units["turn"].append((si, text, toks))
        if role == "user":
            units["user"].append((si, text, toks))
        if role == "user" or cur is None or cur[0] != si:
            if cur:
                units["round"].append(cur)
            cur = (si, text, list(toks))
        else:
            cur = (si, cur[1] + "\n" + text, cur[2] + toks)
    if cur:
        units["round"].append(cur)
    by_session = defaultdict(lambda: ["", []])
    for si, role, content, toks in turn_rows:
        by_session[si][0] += f"{role}: {content}\n"
        by_session[si][1] += toks
    units["session"] = [(si, text, toks) for si, (text, toks) in sorted(by_session.items())]
    return units


def kit_rank(question: str, history: dict, k: int = 4) -> list[tuple[int, str]]:
    """reactor/examples/run_benchmark.py:34-46, re-implemented: (session index, passage text) of the top k turns."""
    words = set(re.findall(r"\w{4,}", question.lower()))
    scored = []
    for si, s in enumerate(history["sessions"]):
        for t in s["turns"]:
            overlap = len(words & set(re.findall(r"\w{4,}", t["content"].lower())))
            if overlap:
                scored.append((overlap, si, f"{t['role']}: {t['content']}"[: 12000 // k]))
    scored.sort(key=lambda x: -x[0])
    return [(si, text) for _, si, text in scored[:k]]


def pack(order: np.ndarray, units: list, budget: int) -> list[tuple[int, str]]:
    """Greedy: units in rank order, each cut to UNIT_CAP, while passage text plus headers fit the budget."""
    used, out = 0, []
    for i in order[:300]:
        si, text, _ = units[i]
        text = text[:UNIT_CAP]
        cost = len(text) + PASSAGE_HEADER
        if used + cost > budget:
            continue
        used += cost
        out.append((si, text))
    return out


def rank_history(line: str, questions: list[dict]) -> list[dict]:
    """Rankings for every question of one history. Sees no gold."""
    history = json.loads(line)
    t0 = time.perf_counter()
    units = build_units(history)
    indexes = {u: BM25([toks for _, _, toks in units[u]]) for u in units}
    t_index = time.perf_counter() - t0
    sids = [s["session_id"] for s in history["sessions"]]
    out = []
    for q in questions:
        row = {"question_id": q["question_id"], "n_sessions": len(sids), "index_s": round(t_index, 3),
               "units": {u: len(units[u]) for u in units}, "methods": {}}
        qt = tokens(q["question"])
        t1 = time.perf_counter()
        kit = kit_rank(q["question"], history)
        row["methods"]["kit"] = {"sessions": list(dict.fromkeys(sids[si] for si, _ in kit)),
                                 "packed": {"kit": [(sids[si], text) for si, text in kit]},
                                 "query_s": round(time.perf_counter() - t1, 4)}
        for u, idx in indexes.items():
            t1 = time.perf_counter()
            sc = idx.scores(qt)
            order = np.argsort(-sc, kind="stable")
            best = {}
            for i in order:
                si = units[u][i][0]
                if si not in best:
                    best[si] = float(sc[i])
                if len(best) >= max(KS):
                    break
            m = {"sessions": [sids[si] for si in best], "top_score": float(sc[order[0]]) if len(order) else 0.0,
                 "max_possible": idx.max_score(qt), "query_s": round(time.perf_counter() - t1, 4)}
            if u != "session":
                m["packed"] = {str(b): [(sids[si], text) for si, text in pack(order, units[u], b)] for b in BUDGETS}
            row["methods"][f"bm25-{u}"] = m
        out.append(row)
    return out


def score(row: dict, gold: dict) -> dict:
    """Gold joined here only, to measure a ranking."""
    ev = set(gold["evidence"])
    ans = norm(gold["answer"]) if not gold["abstain"] else ""
    matchable = 3 <= len(ans) <= 60 and not re.fullmatch(r"\d{1,2}", ans)
    res = {"question_id": row["question_id"], "type": gold["question_type"], "abstain": gold["abstain"],
           "n_evidence": len(ev), "n_sessions": row["n_sessions"], "index_s": row["index_s"], "methods": {}}
    for name, m in row["methods"].items():
        r = {"query_s": m["query_s"]}
        if "top_score" in m:
            r["top_score"] = m["top_score"]
            r["top_score_norm"] = m["top_score"] / m["max_possible"] if m["max_possible"] else 0.0
        if ev:
            for k in KS:
                top = set(m["sessions"][:k])
                r[f"recall@{k}"] = len(ev & top) / len(ev)
                r[f"all@{k}"] = ev <= top
            for b, passages in m.get("packed", {}).items():
                got = {sid for sid, _ in passages}
                r[f"pack{b}_recall"] = len(ev & got) / len(ev)
                r[f"pack{b}_all"] = ev <= got
                r[f"pack{b}_passages"] = len(passages)
                r[f"pack{b}_sessions"] = len(got)
                r[f"pack{b}_chars"] = sum(len(t) for _, t in passages)
                r[f"pack{b}_precision"] = len([s for s in got if s in ev]) / len(got) if got else 0.0
                if matchable:
                    r[f"pack{b}_answer_text"] = any(f" {ans} " in f" {norm(t)} " for _, t in passages)
        res["methods"][name] = r
    return res


def auc(pos: list[float], neg: list[float]) -> float | None:
    """Probability a positive scores above a negative (ties half)."""
    if not pos or not neg:
        return None
    p, n = np.asarray(pos)[:, None], np.asarray(neg)[None, :]
    return float((p > n).mean() + 0.5 * (p == n).mean())


def summarize(results: list[dict]) -> dict:
    out = {}
    methods = sorted({m for r in results for m in r["methods"]})
    groups = defaultdict(list)
    for r in results:
        groups[r["type"]].append(r)
        groups["all"].append(r)
    for name in methods:
        out[name] = {}
        for g, rs in sorted(groups.items()):
            ans = [r["methods"][name] for r in rs if not r["abstain"]]
            row = {"n_answerable": len(ans)}
            keys = sorted({k for a in ans for k in a if k != "query_s" and not k.startswith("top_score")})
            for k in keys:
                vals = [float(a[k]) for a in ans if k in a]
                row[k] = round(float(np.mean(vals)), 3) if vals else None
                if k.endswith("_answer_text"):
                    row[k + "_n"] = len(vals)
            q = [r["methods"][name]["query_s"] for r in rs]
            row["query_ms_p50"] = round(1000 * float(np.percentile(q, 50)), 2)
            row["query_ms_p95"] = round(1000 * float(np.percentile(q, 95)), 2)
            out[name][g] = row
        if name.startswith("bm25"):
            for key in ("top_score", "top_score_norm"):
                pos = [r["methods"][name][key] for r in results if r["abstain"]]
                neg = [r["methods"][name][key] for r in results if not r["abstain"]]
                # AUC of a LOW score predicting abstention: 0.5 = no signal.
                out[name][f"abstain_auc_low_{key}"] = None if not pos else round(1 - auc(pos, neg), 3)
    idx = [r["index_s"] for r in results]
    out["_index_s_per_history"] = {"p50": round(float(np.percentile(idx, 50)), 3),
                                   "p95": round(float(np.percentile(idx, 95)), 3),
                                   "max": round(float(np.max(idx)), 3)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--split", default="dev")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out-dir", type=Path, default=Path("analysis/out"))
    args = ap.parse_args()
    split = args.root / args.split
    questions = defaultdict(list)
    for line in open(split / "questions.jsonl", encoding="utf-8"):
        if line.strip():
            q = json.loads(line)
            questions[q["history_id"]].append(q)
    t0 = time.time()
    rows = []
    with ProcessPoolExecutor(args.workers) as pool:
        futures = []
        with open(split / "histories.jsonl", encoding="utf-8") as f:
            for line in f:
                hid = json.loads(line[:200].split(', "sessions"')[0] + "}")["history_id"]
                futures.append(pool.submit(rank_history, line, questions[hid]))
        for fut in futures:
            rows += fut.result()
    wall = time.time() - t0
    gold = {g["question_id"]: g for g in map(json.loads, open(split / "gold.jsonl", encoding="utf-8"))}
    results = [score(r, gold[r["question_id"]]) for r in rows]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with open(args.out_dir / f"retrieval_{args.split}_per_question.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    summary = summarize(results)
    summary["_wall_s"] = round(wall, 1)
    summary["_workers"] = args.workers
    (args.out_dir / f"retrieval_{args.split}_summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: summary[k] for k in ("_wall_s", "_index_s_per_history")}))


if __name__ == "__main__":
    main()
