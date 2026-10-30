"""Aggregates for the EDA dashboard, written to analysis/out/eda/<dataset>.json (arrays of row objects).

New here: full length and timing distributions, first-person ("personal disclosure") rate per session, an NMF topic
model over user turns, and where gold evidence sits in dev-large histories. Reshaped from earlier outputs:
data_profile.json, retrieval_*_summary.json, question_cues.json, test_structure.txt. Numbers only, plus topic
keywords; no conversation text. Gold is used for diagnostics only.

    python -I analysis/eda.py --root .
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from sklearn.decomposition import NMF
from sklearn.feature_extraction.text import TfidfVectorizer

FMT = "%Y/%m/%d (%a) %H:%M"
LEN_EDGES = [0, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 131072]
GAP_EDGES = [0, 0.25, 1, 3, 6, 12, 24, 48, 96, 168, 336, 720, float("inf")]  # hours between consecutive sessions
SPAN_EDGES = [0, 3, 7, 14, 30, 60, 90, 120, 150, 180, 240, 400]  # days, first session -> question
POS_EDGES = [i / 10 for i in range(11)]
DAY_EDGES = [0, 1, 2, 4, 7, 14, 30, 60, 120, 240, 400]
FIRST_PERSON = {"i", "i'm", "i've", "i'd", "i'll", "my", "me", "mine", "myself"}
WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")
TYPES = ["single-session-user", "single-session-assistant", "multi-session", "knowledge-update", "temporal-reasoning"]
N_TOPICS = 12


def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def content_hash(turns) -> str:
    h = hashlib.sha1()
    for t in turns:
        h.update(t["role"].encode() + b"\0" + t["content"].encode() + b"\1")
    return h.hexdigest()


def hist_rows(values, edges, base: dict, label_fmt=None) -> list[dict]:
    counts, _ = np.histogram(np.asarray(values, dtype=float), bins=edges)
    total = max(int(counts.sum()), 1)
    rows = []
    for i, c in enumerate(counts):
        lo, hi = edges[i], edges[i + 1]
        rows.append({**base, "bin": i, "bin_lo": lo, "bin_hi": None if hi == float("inf") else hi,
                     "label": label_fmt(lo, hi) if label_fmt else f"{lo:g}–{hi:g}",
                     "count": int(c), "share": round(int(c) / total, 5)})
    return rows


def hours_label(lo, hi):
    def f(h):
        return f"{h * 60:g}m" if h < 1 else f"{h:g}h" if h < 48 else f"{h / 24:g}d"
    return f"≥{f(lo)}" if hi == float("inf") else f"{f(lo)}–{f(hi)}"


class SessionFeatures:
    """Per distinct session content: computed once, shared by every history the content appears in."""

    def __init__(self):
        self.by_hash: dict[str, dict] = {}
        self.docs: dict[str, str] = {}

    def add(self, h: str, turns) -> None:
        if h in self.by_hash:
            return
        user = [t["content"] for t in turns if t["role"] == "user"]
        words = [w for u in user for w in WORD.findall(u.lower())]
        chars = sum(len(t["content"]) for t in turns)
        user_chars = sum(len(u) for u in user)
        self.by_hash[h] = {"first_person_per_100": 100 * sum(w in FIRST_PERSON for w in words) / max(len(words), 1),
                           "chars": chars, "user_share": user_chars / max(chars, 1), "turns": len(turns),
                           "starts_with_user": bool(turns) and turns[0]["role"] == "user"}
        self.docs[h] = " ".join(user)[:2000]


def stream_split(root: Path, split: str, feats: SessionFeatures, known: set | None):
    """One pass over a split: distributions, timing, session features, and (with gold) evidence layout."""
    qs = list(read_jsonl(root / split / "questions.jsonl"))
    gold_path = root / split / "gold.jsonl"
    gold = {g["question_id"]: g for g in read_jsonl(gold_path)} if gold_path.exists() else {}
    q_by_h = defaultdict(list)
    for q in qs:
        q_by_h[q["history_id"]].append(q)
    lens = {"user turn": [], "assistant turn": [], "session": []}
    gaps, hours, spans = [], [], []
    hashes, evidence_hashes, new_hashes = set(), set(), set()
    layout = []  # (type, measure, value)
    for h in read_jsonl(root / split / "histories.jsonl"):
        dates = [datetime.strptime(s["date"], FMT) for s in h["sessions"]]
        hashes_here = {}
        for s, d in zip(h["sessions"], dates):
            hsh = content_hash(s["turns"])
            hashes_here[s["session_id"]] = hsh
            hashes.add(hsh)
            feats.add(hsh, s["turns"])
            if known is not None and hsh not in known:
                new_hashes.add(hsh)
            chars = 0
            for t in s["turns"]:
                lens[f"{t['role']} turn"].append(len(t["content"]))
                chars += len(t["content"])
            lens["session"].append(chars)
            hours.append(d.hour)
        gaps += [(b - a).total_seconds() / 3600 for a, b in zip(dates, dates[1:])]
        n = len(h["sessions"])
        index = {s["session_id"]: i for i, s in enumerate(h["sessions"])}
        for q in q_by_h[h["history_id"]]:
            qd = datetime.strptime(q["question_date"], FMT)
            spans.append((qd - dates[0]).total_seconds() / 86400)
            g = gold.get(q["question_id"])
            if not g or g["abstain"]:
                continue
            for e in g["evidence"]:
                i = index[e]
                evidence_hashes.add(hashes_here[e])
                layout.append((g["question_type"], "position", i / max(n - 1, 1)))
                layout.append((g["question_type"], "days_before", (qd - dates[i]).total_seconds() / 86400))
    return {"lens": lens, "gaps": gaps, "hours": hours, "spans": spans, "hashes": hashes,
            "evidence_hashes": evidence_hashes, "new_hashes": new_hashes, "layout": layout}


def topic_rows(feats: SessionFeatures, groups: dict[str, set]) -> list[dict]:
    universe = sorted(set().union(*groups.values()))
    docs = [feats.docs[h] for h in universe]
    vec = TfidfVectorizer(max_features=20000, stop_words="english", min_df=5, max_df=0.3,
                          token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z]+\b", sublinear_tf=True)
    X = vec.fit_transform(docs)
    nmf = NMF(n_components=N_TOPICS, random_state=0, init="nndsvda", max_iter=400)
    W = nmf.fit_transform(X)
    sums = W.sum(axis=1, keepdims=True)
    share = np.divide(W, sums, out=np.zeros_like(W), where=sums > 0)
    vocab = np.array(vec.get_feature_names_out())
    labels = [", ".join(vocab[np.argsort(-c)[:5]]) for c in nmf.components_]
    pos = {h: i for i, h in enumerate(universe)}
    rows = []
    for name, members in groups.items():
        idx = [pos[h] for h in members if sums[pos[h], 0] > 0]
        mean = share[idx].mean(axis=0) if idx else np.zeros(N_TOPICS)
        for k in range(N_TOPICS):
            rows.append({"topic": f"T{k + 1:02d}", "label": labels[k], "group": name, "sessions": len(idx),
                         "share": round(float(mean[k]), 5)})
    return rows


def persona_rows(feats: SessionFeatures, groups: dict[str, set]) -> list[dict]:
    rows = []
    for name, members in groups.items():
        f = [feats.by_hash[h] for h in members]
        fp = np.array([x["first_person_per_100"] for x in f])
        rows.append({"group": name, "sessions": len(f),
                     "first_person_p25": round(float(np.percentile(fp, 25)), 2),
                     "first_person_p50": round(float(np.percentile(fp, 50)), 2),
                     "first_person_p75": round(float(np.percentile(fp, 75)), 2),
                     "session_chars_p50": int(np.median([x["chars"] for x in f])),
                     "user_share_p50": round(float(np.median([x["user_share"] for x in f])), 4),
                     "turns_p50": float(np.median([x["turns"] for x in f])),
                     "starts_with_user": round(float(np.mean([x["starts_with_user"] for x in f])), 4)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--out", type=Path, default=Path("analysis/out/eda"))
    args = ap.parse_args()
    a = args.root / "analysis/out"
    t0 = time.time()
    feats = SessionFeatures()
    res = {}
    for split in ("dev", "dev-large"):
        res[split] = stream_split(args.root, split, feats, None)
        print(f"{split}: {time.time() - t0:.0f}s", flush=True)
    known = res["dev"]["hashes"] | res["dev-large"]["hashes"]
    res["test"] = stream_split(args.root, "test", feats, known)
    print(f"test: {time.time() - t0:.0f}s", flush=True)
    out: dict[str, list[dict]] = defaultdict(list)

    # lengths and timing
    for split, r in res.items():
        for unit, vals in r["lens"].items():
            out["lengths"] += hist_rows(vals, LEN_EDGES, {"split": split, "unit": unit},
                                        lambda lo, hi: f"{lo:,.0f}–{hi:,.0f}")
        out["timing"] += hist_rows(r["gaps"], GAP_EDGES, {"split": split, "measure": "gap_hours"}, hours_label)
        out["timing"] += hist_rows(r["spans"], SPAN_EDGES, {"split": split, "measure": "span_days"},
                                   lambda lo, hi: f"{lo:g}–{hi:g} d")
        out["timing"] += hist_rows(r["hours"], list(range(25)), {"split": split, "measure": "hour_of_day"},
                                   lambda lo, hi: f"{int(lo):02d}:00")

    # evidence layout in dev-large
    for t in TYPES:
        for measure, edges, fmt in (("position", POS_EDGES, lambda lo, hi: f"{lo:.1f}–{hi:.1f}"),
                                    ("days_before", DAY_EDGES, lambda lo, hi: f"{lo:g}–{hi:g} d")):
            vals = [v for tt, m, v in res["dev-large"]["layout"] if tt == t and m == measure]
            out["evidence_layout"] += hist_rows(vals, edges, {"type": t, "measure": measure}, fmt)

    # persona signal and topics
    dl = res["dev-large"]
    groups = {"dev-large evidence": dl["evidence_hashes"],
              "dev-large other": dl["hashes"] - dl["evidence_hashes"],
              "test new": res["test"]["new_hashes"],
              "test reused": res["test"]["hashes"] - res["test"]["new_hashes"]}
    out["persona"] = persona_rows(feats, groups)
    out["topics"] = topic_rows(feats, groups)
    print(f"topics: {time.time() - t0:.0f}s", flush=True)

    # reshaped earlier outputs
    prof = json.loads((a / "data_profile.json").read_text())
    for split, v in prof["splits"].items():
        c, s, reuse = v["counts"], v["sizes"], v["session_reuse"]
        out["splits"].append({
            "split": split, "histories": c["histories"], "questions": c["questions"],
            "questions_per_history": round(c["questions"] / c["histories"], 2),
            "sessions": c["sessions_total"], "sessions_per_history_p50": s["history_sessions"]["p50"],
            "distinct_sessions": c["distinct_session_ids"],
            "reused_session_share": round(reuse["sessions_in_more_than_one_history"] / c["distinct_session_ids"], 4),
            "turns": c["turns_user"] + c["turns_assistant"],
            "user_char_share": round(c["chars_user"] / (c["chars_user"] + c["chars_assistant"]), 4),
            "history_chars_p50": s["history_chars"]["p50"], "session_chars_p50": s["session_chars"]["p50"],
            "user_turn_chars_p50": s["turn_chars_user"]["p50"],
            "assistant_turn_chars_p50": s["turn_chars_assistant"]["p50"],
            "span_days_p50": s["history_span_days"]["p50"],
            "question_chars_mean": s["question_chars"]["mean"]})
    g = prof["splits"]["dev"]["gold"]["types"]
    for t in TYPES:
        x = g[t]
        ans = x["n"] - x["abstain"]
        row = {"type": t, "questions": x["n"], "abstain": x["abstain"], "answerable": ans,
               "evidence_chars_p50": x["evidence_chars_total"]["p50"],
               "over_16k": x["evidence_total_over_16k"], "over_32k": x["evidence_total_over_32k"],
               "over_16k_share": round(x["evidence_total_over_16k"] / ans, 4),
               "over_32k_share": round(x["evidence_total_over_32k"] / ans, 4),
               "answer_matchable": x["answer_string_matchable"], "answer_found": x["answer_found_in_evidence"],
               "found_user": x["answer_found_role"].get("user", 0), "found_both": x["answer_found_role"].get("both", 0),
               "found_assistant": x["answer_found_role"].get("assistant", 0),
               "relative_date_share": round(x["evidence_with_user_relative_date"] / ans, 4)}
        for k in range(1, 7):
            row[f"evidence_{k}"] = x["evidence_sessions"].get(str(k), 0)
        out["gold_types"].append(row)
    marker_groups = (("dev", "dev"), ("dev-large", "dev-large, not in dev"), ("test", "test, new"))
    for split, name in marker_groups:
        v = prof["splits"][split]
        n = v["other_counts"]["distinct_sessions_marker_scanned"]
        for marker in ("relative_date", "explicit_date", "update_marker"):
            k = v["sessions_with_marker_per_role"][marker].get("user", 0)
            out["markers"].append({"group": name, "marker": marker, "sessions": n, "with_marker": k,
                                   "share": round(k / n, 4)})
    for split in ("dev", "dev-large"):
        summ = json.loads((a / f"retrieval_{split}_summary.json").read_text())
        for method, by_type in summ.items():
            if method.startswith("_"):
                continue
            for t, r in by_type.items():
                if not isinstance(r, dict):
                    continue
                for k in (1, 3, 5, 10, 20, 50):
                    out["retrieval"].append({"split": split, "method": method, "type": t, "k": k,
                                             "recall": r[f"recall@{k}"], "all_found": r[f"all@{k}"]})
                for budget, key in (("14k", "pack14000"), ("30k", "pack30000"), ("kit 4 turns", "packkit")):
                    if f"{key}_recall" in r:
                        out["packing"].append({
                            "split": split, "method": method, "type": t, "budget": budget,
                            "recall": r[f"{key}_recall"], "all_found": r[f"{key}_all"],
                            "answer_text": r.get(f"{key}_answer_text"), "precision": r[f"{key}_precision"],
                            "sessions": r[f"{key}_sessions"], "chars": r[f"{key}_chars"]})
    cues = json.loads((a / "question_cues.json").read_text())
    for t, row in cues["dev"].items():
        for cue, v in row.items():
            if cue != "n":
                out["cues"].append({"group": f"dev {t}" if t != "all" else "dev all", "cue": cue, "share": v,
                                    "questions": row["n"]})
    for cue, v in cues["test"].items():
        if cue != "n":
            out["cues"].append({"group": "test", "cue": cue, "share": v, "questions": cues["test"]["n"]})
    for line in (a / "test_structure.txt").read_text().splitlines():
        r = json.loads(line)
        if "history_id" in r:
            out["test_histories"].append({
                "history_id": r["history_id"], "sessions": r["sessions"], "new_sessions": r["new"],
                "reused_sessions": r["sessions"] - r["new"], "new_chars_p50": r["new_chars_p50"],
                "reused_chars_p50": r["reused_chars_p50"], "first_new_day": r["new_day_min_max"][0],
                "last_new_day": r["new_day_min_max"][1]})

    args.out.mkdir(parents=True, exist_ok=True)
    for name, rows in out.items():
        (args.out / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False))
        print(f"{name}: {len(rows)} rows", flush=True)
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
