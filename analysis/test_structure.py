"""Test-split structure from inputs only: per history, how many sessions reuse dev/dev-large content vs are new,
and where the new ones sit in time. Diagnostic: describes construction; must never become a retrieval feature.

    python -I analysis/test_structure.py --root .
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np

FMT = "%Y/%m/%d (%a) %H:%M"


def h(turns) -> str:
    x = hashlib.sha1()
    for t in turns:
        x.update(t["role"].encode() + b"\0" + t["content"].encode() + b"\1")
    return x.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("."))
    args = ap.parse_args()
    known = set()
    for split in ("dev", "dev-large"):
        for line in open(args.root / split / "histories.jsonl"):
            for s in json.loads(line)["sessions"]:
                known.add(h(s["turns"]))
    rows = []
    for line in open(args.root / "test/histories.jsonl"):
        hist = json.loads(line)
        ss = hist["sessions"]
        first = datetime.strptime(ss[0]["date"], FMT)
        new = [i for i, s in enumerate(ss) if h(s["turns"]) not in known]
        new_days = [(datetime.strptime(ss[i]["date"], FMT) - first).days for i in new]
        new_chars = [sum(len(t["content"]) for t in ss[i]["turns"]) for i in new]
        old_chars = [sum(len(t["content"]) for t in s["turns"]) for i, s in enumerate(ss) if i not in set(new)]
        rows.append({"history_id": hist["history_id"], "sessions": len(ss), "new": len(new),
                     "new_position_p10_p50_p90": [round(float(np.percentile(new, p)) / (len(ss) - 1), 2) for p in (10, 50, 90)],
                     "new_day_min_max": [min(new_days), max(new_days)],
                     "new_chars_p50": int(np.median(new_chars)), "reused_chars_p50": int(np.median(old_chars))})
    for r in rows:
        print(json.dumps(r))
    n = [r["new"] for r in rows]
    print(json.dumps({"new_sessions_total": sum(n), "per_history_min_mean_max": [min(n), round(sum(n) / len(n), 1), max(n)]}))


if __name__ == "__main__":
    main()
