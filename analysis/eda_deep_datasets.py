"""Reshape analysis/out/deep_dive.json into dashboard datasets (analysis/out/eda/<name>.json, arrays of rows).

    python -I analysis/eda_deep_datasets.py
"""
from __future__ import annotations

import json
from pathlib import Path

TYPES = ["single-session-user", "single-session-assistant", "multi-session", "knowledge-update", "temporal-reasoning"]
SIGNALS = {"top_score": "Top BM25 score (lower)", "absent_frac": "Question terms absent from history",
           "absent_idf_frac": "Absent terms, weighted by rarity", "absent_user_frac": "Question terms absent from user turns"}


def main():
    root = Path(__file__).resolve().parent / "out"
    d = json.loads((root / "deep_dive.json").read_text())
    out: dict[str, list[dict]] = {}

    out["abstention_signals"] = [
        {"split": split, "signal": key, "label": SIGNALS[key], "auc": v[key]["auc"],
         "abstain_mean": v[key]["abstain_mean"], "answerable_mean": v[key]["answerable_mean"],
         "flag_abstain": v["any_absent_term"]["abstain"], "flag_answerable": v["any_absent_term"]["answerable"]}
        for split, v in d["abstention"].items() for key in SIGNALS]

    out["evidence_ranks"] = []
    for t in TYPES:
        m = d["misses"][t]
        out["evidence_ranks"].append({
            "type": t, "evidence_sessions": m["evidence_sessions"], "missed_top10": m["missed_top10"],
            "miss_share": round(m["missed_top10"] / m["evidence_sessions"], 4),
            "rank_p50": m["rank"]["p50"], "rank_p90": m["rank"]["p90"],
            "coverage_hit_p50": m["coverage_hit"]["p50"] if m["coverage_hit"] else None,
            "coverage_miss_p50": m["coverage_miss"]["p50"] if m["coverage_miss"] else None,
            "zero_coverage_misses": m["miss_with_zero_coverage"]})

    out["crowding"] = []
    for split, v in d["crowding"].items():
        out["crowding"].append({
            "split": split, "crowd90_p50": v["crowd90"]["p50"], "crowd90_p90": v["crowd90"]["p90"],
            "crowd75_p50": v["crowd75"]["p50"], "crowd75_p90": v["crowd75"]["p90"], "gap_p50": v["rel_gap"]["p50"],
            "precision_at_10_mean": v.get("precision_at_10", {}).get("mean"),
            "top10_new_share_mean": v.get("test_top10_new_share", {}).get("mean"),
            "history_new_share": v.get("test_history_new_share")})

    out["date_phrasing"] = [
        {"group": group, "phrasing": k, "share": share, "sessions": v["sessions"]}
        for group, v in d["relative_dates"].items() for k, share in v.items() if k not in ("sessions",)]

    f = d["assistant_answer_format"]
    out["answer_format"] = [{"format": k, "questions": n, "share": round(n / f["found"], 4),
                             "offset_p50": f["offset_in_turn"]["p50"], "turn_chars_p50": f["turn_chars"]["p50"]}
                            for k, n in sorted(f["format"].items(), key=lambda kv: -kv[1])]

    out["recurrence"] = []
    for split, v in d["recurrence"].items():
        for group, row in v.items():
            out["recurrence"].append({
                "group": f"{split}, {'all' if group == 'all' else group}", "share_2plus": row["share_2plus"],
                "multiplicity_mean": row["multiplicity"]["mean"], "multiplicity_p90": row["multiplicity"]["p90"],
                "spread_days_p50": (row.get("spread_days_when_2plus") or {}).get("p50")})

    for name, rows in out.items():
        (root / "eda" / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False))
        print(name, len(rows))


if __name__ == "__main__":
    main()
