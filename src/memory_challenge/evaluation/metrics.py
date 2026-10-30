"""Retrieval metrics against dev gold (the only module that reads gold besides the official scorer).

Session-level: a gold session among the ranked or packed sessions counts as a hit. A hit shows the session was
retrieved, not that the packed span contains the answer; "answer text in pack" (gold answer string found verbatim in
the packed text, for short answers only) is reported as a rough sufficiency proxy.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

from ..text import normalize

KS = (1, 3, 5, 10, 20, 50)


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    i = min(len(v) - 1, max(0, round(p / 100 * (len(v) - 1))))
    return v[i]


def score_record(rec: dict, gold: dict) -> dict:
    ev = set(gold["evidence"])
    out = {"question_id": rec["question_id"], "type": gold["question_type"], "abstain": gold["abstain"],
           "n_evidence": len(ev), "passages": rec["passages"], "message_chars": rec["message_chars"],
           "retrieval_ms": rec["retrieval_s"] * 1000, "pack_ms": rec["pack_s"] * 1000,
           "index_build_ms": rec["index_build_s"] * 1000, "cold": rec["cold"]}
    if ev:
        ranked = rec["ranked_sessions"]
        for k in KS:
            top = set(ranked[:k])
            out[f"recall@{k}"] = len(ev & top) / len(ev)
            out[f"all@{k}"] = float(ev <= top)
        packed = set(rec["packed_sessions"])
        hit = len(ev & packed)
        out["pack_recall"] = hit / len(ev)
        out["pack_all"] = float(ev <= packed)
        out["pack_precision"] = hit / len(packed) if packed else 0.0
        p, r = out["pack_precision"], out["pack_recall"]
        out["pack_f1"] = 2 * p * r / (p + r) if p + r else 0.0
        ans = normalize(gold["answer"])
        if 3 <= len(ans) <= 60 and not ans.isdigit():
            out["answer_text_in_pack"] = float(f" {ans} " in f" {normalize(rec['packed_text'])} ")
    return out


def summarize(scored: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in scored:
        groups[s["type"]].append(s)
        groups["all"].append(s)
    out = {}
    for name, rows in sorted(groups.items()):
        ans = [r for r in rows if not r["abstain"]]
        row = {"n": len(rows), "answerable": len(ans)}
        keys = [f"recall@{k}" for k in KS] + [f"all@{k}" for k in KS] + \
               ["pack_recall", "pack_all", "pack_precision", "pack_f1", "answer_text_in_pack"]
        for k in keys:
            vals = [r[k] for r in ans if k in r]
            row[k] = round(statistics.fmean(vals), 4) if vals else None
            if k == "answer_text_in_pack":
                row["answer_text_n"] = len(vals)
        row["passages_mean"] = round(statistics.fmean(r["passages"] for r in rows), 2)
        row["message_chars_mean"] = round(statistics.fmean(r["message_chars"] for r in rows), 1)
        row["message_chars_max"] = max(r["message_chars"] for r in rows)
        out[name] = row
    lat = {
        "index_build_ms_p50": _pct([s["index_build_ms"] for s in scored if s["cold"]], 50),
        "index_build_ms_p95": _pct([s["index_build_ms"] for s in scored if s["cold"]], 95),
        "retrieval_cold_ms_p50": _pct([s["retrieval_ms"] for s in scored if s["cold"]], 50),
        "retrieval_warm_ms_p50": _pct([s["retrieval_ms"] for s in scored if not s["cold"]], 50),
        "retrieval_ms_p50": _pct([s["retrieval_ms"] for s in scored], 50),
        "retrieval_ms_p95": _pct([s["retrieval_ms"] for s in scored], 95),
        "pack_ms_p50": _pct([s["pack_ms"] for s in scored], 50),
        "pack_ms_p95": _pct([s["pack_ms"] for s in scored], 95),
    }
    out["_latency"] = {k: (round(v, 2) if v is not None else None) for k, v in lat.items()}
    return out


TYPE_ORDER = ["all", "single-session-user", "single-session-assistant", "multi-session", "knowledge-update",
              "temporal-reasoning"]


def markdown_table(summary: dict, title: str) -> str:
    cols = ["n", "recall@5", "recall@10", "all@10", "all@50", "pack_recall", "pack_all", "pack_precision", "pack_f1",
            "answer_text_in_pack", "passages_mean", "message_chars_mean"]
    lines = [f"### {title}", "", "| type | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 1)]
    for t in TYPE_ORDER:
        if t in summary:
            r = summary[t]
            lines.append(f"| {t} | " + " | ".join("—" if r.get(c) is None else str(r.get(c)) for c in cols) + " |")
    lat = summary.get("_latency", {})
    lines += ["", "Latency (ms): " + ", ".join(f"{k} {v}" for k, v in lat.items())]
    return "\n".join(lines)
