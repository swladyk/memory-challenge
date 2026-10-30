"""Deeper EDA: abstention signals, why retrieval misses evidence, near-miss crowding, relative-date phrasing, how
assistant answers are formatted, duplicate sessions, and how often question entities recur across sessions.

rank_history() ranks and computes question-level signals without gold. diagnose() looks only at the evidence
sessions it is handed, for diagnostics; main() joins gold afterwards. Test uses inputs only. Writes
analysis/out/deep_dive.json.

    python -I analysis/deep_dive.py --root . --workers 8
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieval_baselines import BM25, auc, norm, stem, tokens  # noqa: E402  (our own module)

FMT = "%Y/%m/%d (%a) %H:%M"
GENERIC = {stem(w) for w in ("remind", "previous", "chat", "conversation", "talk", "talked", "discuss", "discussed",
                              "mention", "mentioned", "told", "said", "remember", "recall", "ask", "asked")}
DAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
          "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
MONTHS_FULL = "january|february|march|april|may|june|july|august|september|october|november|december"
NUM = r"\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|a few|a couple of|couple of|several"
FAMILIES = {
    "today / tonight / this morning": r"\b(?:today|tonight|this (?:morning|afternoon|evening))\b",
    "yesterday": r"\byesterday\b",
    "tomorrow": r"\btomorrow\b",
    "last <weekday>": rf"\blast (?:{DAYS})\b",
    "this / next <weekday>": rf"\b(?:this|next|coming) (?:{DAYS})\b",
    "on <weekday>": rf"\bon (?:{DAYS})s?\b",
    "last week / weekend / month / year": r"\blast (?:week|weekend|month|year)\b",
    "this week / weekend / month / year": r"\bthis (?:week|weekend|month|year)\b",
    "next week / weekend / month / year": r"\bnext (?:week|weekend|month|year)\b",
    "N days / weeks / months ago": rf"\b(?:{NUM}) (?:day|week|month|year)s? ago\b",
    "recently / the other day / just": r"\b(?:recently|the other day|earlier this (?:week|month|year)|just (?:got back|finished|started|bought|got))\b",
    "in / for N days / weeks (ahead)": rf"\b(?:in|for|over the next) (?:{NUM}) (?:day|week|month)s?\b",
    "<Month> <day>": rf"\b(?:{MONTHS})\.? \d{{1,2}}(?:st|nd|rd|th)?\b",
    "in / since / by <Month>": rf"\b(?:in|since|until|by|early|late|mid) (?:{MONTHS_FULL})\b",
}
FAMILY_RX = {k: re.compile(v, re.I) for k, v in FAMILIES.items()}


def content_hash(turns) -> str:
    h = hashlib.sha1()
    for t in turns:
        h.update(t["role"].encode() + b"\0" + t["content"].encode() + b"\1")
    return h.hexdigest()


def families(text: str) -> list[str]:
    return [k for k, rx in FAMILY_RX.items() if rx.search(text)]


def line_format(line: str) -> str:
    s = line.strip()
    if "|" in s:
        return "table row"
    if re.match(r"^\d+[.)]\s", s):
        return "numbered item"
    if re.match(r"^[-*•]\s", s):
        return "bullet item"
    if s.startswith("#"):
        return "heading"
    if "**" in s:
        return "bold text in a paragraph"
    return "plain paragraph"


def rank_history(line: str, questions: list[dict], diag: dict | None) -> dict:
    """Question-level signals and rankings (no gold); diagnostics on the evidence sessions in ``diag`` if given."""
    h = json.loads(line)
    sessions = h["sessions"]
    dates = [datetime.strptime(s["date"], FMT) for s in sessions]
    turn_sess, turn_role, turn_toks = [], [], []
    sess_toks, sess_user_toks = [], []
    for si, s in enumerate(sessions):
        all_t, user_t = set(), set()
        for t in s["turns"]:
            tk = tokens(t["content"])
            turn_sess.append(si)
            turn_role.append(t["role"])
            turn_toks.append(tk)
            all_t.update(tk)
            if t["role"] == "user":
                user_t.update(tk)
        sess_toks.append(all_t)
        sess_user_toks.append(user_t)
    n = len(sessions)
    df = Counter(t for st in sess_toks for t in st)
    idf = lambda t: float(np.log((n + 1) / (df.get(t, 0) + 1)))  # noqa: E731
    bm = BM25(turn_toks)
    turn_sess_arr = np.asarray(turn_sess)
    hashes = [content_hash(s["turns"]) for s in sessions]
    out = {"history_id": h["history_id"], "n_sessions": n, "hashes": hashes, "questions": []}
    dup_groups = [ids for ids in _groups(hashes) if len(ids) > 1]
    out["dup_groups"] = len(dup_groups)
    for q in questions:
        qt = [t for t in dict.fromkeys(tokens(q["question"])) if t not in GENERIC]
        sc = bm.scores(qt) if qt else np.zeros(len(turn_toks), dtype=np.float32)
        best = np.full(n, -1.0)
        best_turn = np.full(n, -1)
        order = np.argsort(-sc, kind="stable")
        for i in order:
            si = turn_sess_arr[i]
            if best[si] < 0:
                best[si], best_turn[si] = sc[i], i
        rank_order = np.argsort(-best, kind="stable")
        s_sorted = best[rank_order]
        s1 = float(s_sorted[0]) if n else 0.0
        absent = [t for t in qt if df.get(t, 0) == 0]
        absent_user = [t for t in qt if not any(t in u for u in sess_user_toks)]
        tot_idf = sum(idf(t) for t in qt) or 1.0
        present = sorted((t for t in qt if df.get(t, 0) > 0), key=lambda t: -idf(t))
        top2 = present[:2]
        mult_ids = [si for si in range(n) if top2 and all(t in sess_user_toks[si] for t in top2)]
        spread = ((max(dates[i] for i in mult_ids) - min(dates[i] for i in mult_ids)).total_seconds() / 86400
                  if len(mult_ids) > 1 else 0.0)
        rec = {"question_id": q["question_id"], "n_terms": len(qt),
               "absent_frac": len(absent) / len(qt) if qt else 0.0,
               "absent_user_frac": len(absent_user) / len(qt) if qt else 0.0,
               "absent_idf_frac": sum(idf(t) for t in absent) / tot_idf if qt else 0.0,
               "absent_terms": absent[:6],
               "top_score": s1,
               "crowd90": int((best >= 0.9 * s1).sum()) - 1 if s1 > 0 else 0,
               "crowd75": int((best >= 0.75 * s1).sum()) - 1 if s1 > 0 else 0,
               "rel_gap": float((s_sorted[0] - s_sorted[1]) / s_sorted[0]) if n > 1 and s1 > 0 else 0.0,
               "top10": [sessions[i]["session_id"] for i in rank_order[:10]],
               "top10_idx": [int(i) for i in rank_order[:10]],
               "multiplicity": len(mult_ids), "mult_spread_days": spread}
        if diag and q["question_id"] in diag:
            rec["diag"] = diagnose(diag[q["question_id"]], sessions, rank_order, best, best_turn, turn_role,
                                   sess_toks, qt, hashes)
        out["questions"].append(rec)
    out["families"] = [families(" ".join(t["content"] for t in s["turns"] if t["role"] == "user")) for s in sessions]
    return out


def _groups(hashes):
    g = defaultdict(list)
    for i, x in enumerate(hashes):
        g[x].append(i)
    return g.values()


def diagnose(d: dict, sessions, rank_order, best, best_turn, turn_role, sess_toks, qt, hashes) -> dict:
    """Diagnostics on the gold evidence sessions of one question (gold enters here only)."""
    rank_of = {int(si): r + 1 for r, si in enumerate(rank_order)}
    index = {s["session_id"]: i for i, s in enumerate(sessions)}
    ev = []
    for sid in d["evidence"]:
        si = index[sid]
        s = sessions[si]
        cov = len([t for t in qt if t in sess_toks[si]]) / len(qt) if qt else 0.0
        user = " ".join(t["content"] for t in s["turns"] if t["role"] == "user")
        dup = [sessions[j]["session_id"] for j, x in enumerate(hashes) if x == hashes[si] and j != si]
        # the user turn sharing most question terms, as a short excerpt for the report
        ut = [t["content"] for t in s["turns"] if t["role"] == "user"]
        snippet = max(ut, key=lambda c: len(set(tokens(c)) & set(qt)))[:220] if ut else ""
        ev.append({"rank": rank_of[si], "coverage": cov, "best_role": turn_role[best_turn[si]] if best_turn[si] >= 0 else None,
                   "chars": sum(len(t["content"]) for t in s["turns"]), "families": families(user),
                   "duplicates_in_history": dup, "snippet": " ".join(snippet.split())})
    fmt = None
    ans = norm(d["answer"])
    if d["type"] == "single-session-assistant" and 3 <= len(ans) <= 60:
        for sid in d["evidence"]:
            for t in sessions[index[sid]]["turns"]:
                if t["role"] != "assistant":
                    continue
                lines = t["content"].split("\n")
                for li, line in enumerate(lines):
                    if f" {ans} " in f" {norm(line)} ":
                        offset = sum(len(x) + 1 for x in lines[:li]) / max(len(t["content"]), 1)
                        fmt = {"format": line_format(line), "offset": offset, "turn_chars": len(t["content"])}
                        break
                if fmt:
                    break
            if fmt:
                break
    return {"evidence": ev, "answer_format": fmt}


def run_split(root: Path, split: str, workers: int, with_diag: bool):
    qs = defaultdict(list)
    for line in open(root / split / "questions.jsonl"):
        q = json.loads(line)
        qs[q["history_id"]].append(q)
    gold = {}
    if with_diag and (root / split / "gold.jsonl").exists():
        gold = {g["question_id"]: g for g in map(json.loads, open(root / split / "gold.jsonl"))}
    results = []
    with ProcessPoolExecutor(workers) as pool:
        futs = []
        for line in open(root / split / "histories.jsonl"):
            hid = json.loads(line[:200].split(', "sessions"')[0] + "}")["history_id"]
            diag = {q["question_id"]: {"evidence": gold[q["question_id"]]["evidence"], "answer": gold[q["question_id"]]["answer"],
                                       "type": gold[q["question_id"]]["question_type"]}
                    for q in qs[hid] if q["question_id"] in gold and not gold[q["question_id"]]["abstain"]} if gold else None
            futs.append(pool.submit(rank_history, line, qs[hid], diag))
        for f in futs:
            results.append(f.result())
    return results, gold


def dist(vals) -> dict | None:
    if not len(vals):
        return None
    a = np.asarray(vals, dtype=float)
    return {"n": int(a.size), "mean": round(float(a.mean()), 3),
            **{f"p{p}": round(float(np.percentile(a, p)), 3) for p in (10, 25, 50, 75, 90)}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", type=Path, default=Path("analysis/out/deep_dive.json"))
    args = ap.parse_args()
    t0 = time.time()
    res, golds = {}, {}
    for split in ("dev", "dev-large", "test"):
        res[split], golds[split] = run_split(args.root, split, args.workers, split != "test")
        print(f"{split}: {time.time() - t0:.0f}s", flush=True)
    known = {x for split in ("dev", "dev-large") for h in res[split] for x in h["hashes"]}
    report: dict = {}

    # A. abstention signals (dev, dev-large)
    report["abstention"] = {}
    for split in ("dev", "dev-large"):
        g = golds[split]
        recs = [q for h in res[split] for q in h["questions"]]
        lab = {q["question_id"]: g[q["question_id"]]["abstain"] for q in recs}
        row = {}
        for key, sign in (("absent_frac", 1), ("absent_user_frac", 1), ("absent_idf_frac", 1), ("top_score", -1)):
            pos = [sign * q[key] for q in recs if lab[q["question_id"]]]
            neg = [sign * q[key] for q in recs if not lab[q["question_id"]]]
            row[key] = {"auc": round(auc(pos, neg), 3), "abstain_mean": round(float(np.mean([q[key] for q in recs if lab[q["question_id"]]])), 3),
                        "answerable_mean": round(float(np.mean([q[key] for q in recs if not lab[q["question_id"]]])), 3)}
        row["any_absent_term"] = {
            "abstain": round(float(np.mean([q["absent_frac"] > 0 for q in recs if lab[q["question_id"]]])), 3),
            "answerable": round(float(np.mean([q["absent_frac"] > 0 for q in recs if not lab[q["question_id"]]])), 3)}
        row["any_absent_in_user_turns"] = {
            "abstain": round(float(np.mean([q["absent_user_frac"] > 0 for q in recs if lab[q["question_id"]]])), 3),
            "answerable": round(float(np.mean([q["absent_user_frac"] > 0 for q in recs if not lab[q["question_id"]]])), 3)}
        report["abstention"][split] = row
    qtext = {json.loads(l)["question_id"]: json.loads(l)["question"] for l in open(args.root / "dev/questions.jsonl")}
    report["abstention_examples"] = [
        {"question": qtext[q["question_id"]], "absent_terms": q["absent_terms"], "absent_user_frac": round(q["absent_user_frac"], 2)}
        for h in res["dev"] for q in h["questions"] if golds["dev"][q["question_id"]]["abstain"]][:12]

    # B. why retrieval misses evidence (dev-large, by type)
    report["misses"] = {}
    examples = []
    g = golds["dev-large"]
    for t in ("single-session-user", "single-session-assistant", "multi-session", "knowledge-update", "temporal-reasoning"):
        ev = [(q, e) for h in res["dev-large"] for q in h["questions"] if "diag" in q and g[q["question_id"]]["question_type"] == t
              for e in q["diag"]["evidence"]]
        hit = [e for _, e in ev if e["rank"] <= 10]
        miss = [e for _, e in ev if e["rank"] > 10]
        report["misses"][t] = {
            "evidence_sessions": len(ev), "missed_top10": len(miss),
            "rank": dist([e["rank"] for _, e in ev]),
            "coverage_hit": dist([e["coverage"] for e in hit]), "coverage_miss": dist([e["coverage"] for e in miss]),
            "miss_with_zero_coverage": sum(e["coverage"] == 0 for e in miss),
            "best_role_hit": dict(Counter(e["best_role"] for e in hit)), "best_role_miss": dict(Counter(e["best_role"] for e in miss)),
            "chars_hit_p50": float(np.median([e["chars"] for e in hit])) if hit else None,
            "chars_miss_p50": float(np.median([e["chars"] for e in miss])) if miss else None}
        if t in ("multi-session", "temporal-reasoning"):
            for q, e in ev:
                if e["rank"] > 50 and len(examples) < 8:
                    examples.append({"type": t, "question": next(json.loads(l)["question"] for l in open(args.root / "dev-large/questions.jsonl")
                                                                if json.loads(l)["question_id"] == q["question_id"]),
                                     "rank": e["rank"], "coverage": round(e["coverage"], 2), "snippet": e["snippet"]})
    report["miss_examples"] = examples

    # C. crowding at the top of the ranking
    report["crowding"] = {}
    for split in ("dev", "dev-large", "test"):
        recs = [q for h in res[split] for q in h["questions"]]
        row = {"crowd90": dist([q["crowd90"] for q in recs]), "crowd75": dist([q["crowd75"] for q in recs]),
               "rel_gap": dist([q["rel_gap"] for q in recs])}
        if split == "test":
            by_h = {h["history_id"]: h for h in res[split]}
            new_share = []
            for h in res[split]:
                for q in h["questions"]:
                    new_share.append(np.mean([h["hashes"][i] not in known for i in q["top10_idx"]]))
            row["test_top10_new_share"] = dist(new_share)
            row["test_history_new_share"] = round(float(np.mean([np.mean([x not in known for x in h["hashes"]]) for h in res[split]])), 3)
        else:
            gg = golds[split]
            prec = [len(set(q["top10"]) & set(gg[q["question_id"]]["evidence"])) / 10 for q in recs if not gg[q["question_id"]]["abstain"]]
            row["precision_at_10"] = dist(prec)
        report["crowding"][split] = row

    # D. relative-date phrasing
    report["relative_dates"] = {}
    test_new = [fam for h in res["test"] for x, fam in zip(h["hashes"], h["families"]) if x not in known]
    tr_ev = [e["families"] for h in res["dev-large"] for q in h["questions"] if "diag" in q
             and g[q["question_id"]]["question_type"] == "temporal-reasoning" for e in q["diag"]["evidence"]]
    all_dl = [fam for h in res["dev-large"][:60] for fam in h["families"]]  # first 60 histories, a sample of all sessions
    for name, group in (("test, new sessions", test_new), ("dev-large, temporal evidence", tr_ev), ("dev-large, sample of all sessions", all_dl)):
        c = Counter(f for fam in group for f in fam)
        report["relative_dates"][name] = {"sessions": len(group), "any": round(float(np.mean([bool(f) for f in group])), 3),
                                          **{k: round(c[k] / len(group), 3) for k in FAMILIES}}

    # E. how assistant answers are formatted (dev single-session-assistant)
    fmts = [q["diag"]["answer_format"] for h in res["dev"] for q in h["questions"] if "diag" in q and q["diag"]["answer_format"]]
    report["assistant_answer_format"] = {"found": len(fmts), "format": dict(Counter(f["format"] for f in fmts)),
                                         "offset_in_turn": dist([f["offset"] for f in fmts]),
                                         "turn_chars": dist([f["turn_chars"] for f in fmts])}

    # F. duplicate session contents inside a history
    report["duplicates"] = {}
    for split in ("dev", "dev-large", "test"):
        row = {"histories_with_duplicates": sum(h["dup_groups"] > 0 for h in res[split]), "histories": len(res[split])}
        if split != "test":
            ev = [e for h in res[split] for q in h["questions"] if "diag" in q for e in q["diag"]["evidence"]]
            row["evidence_sessions"] = len(ev)
            row["evidence_with_duplicate_content"] = sum(bool(e["duplicates_in_history"]) for e in ev)
        report["duplicates"][split] = row

    # G. how often a question's two rarest terms recur together in user turns
    report["recurrence"] = {}
    for split in ("dev-large", "test"):
        recs = [q for h in res[split] for q in h["questions"]]
        row = {"all": {"multiplicity": dist([q["multiplicity"] for q in recs]),
                       "spread_days_when_2plus": dist([q["mult_spread_days"] for q in recs if q["multiplicity"] > 1]),
                       "share_2plus": round(float(np.mean([q["multiplicity"] > 1 for q in recs])), 3)}}
        if split == "dev-large":
            for t in ("single-session-user", "multi-session", "knowledge-update", "temporal-reasoning"):
                rr = [q for q in recs if g[q["question_id"]]["question_type"] == t]
                row[t] = {"multiplicity": dist([q["multiplicity"] for q in rr]),
                          "share_2plus": round(float(np.mean([q["multiplicity"] > 1 for q in rr])), 3)}
        report["recurrence"][split] = row

    args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(f"wrote {args.out} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
