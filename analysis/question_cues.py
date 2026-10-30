"""Surface cues in question text, per dev type and for test (no test labels exist; this only compares phrasing).

    python -I analysis/question_cues.py --root . --out analysis/out/question_cues.json
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

CUES = {
    "assistant_ref": r"\b(?:you (?:said|told|suggested|recommended|mentioned|gave|provided|listed)|remind me|"
                     r"our (?:previous|last|earlier) (?:chat|conversation)|we (?:talked|discussed|chatted))\b",
    "count_total": r"\b(?:how many|how much|total|in total|combined|altogether|sum)\b",
    "duration": r"\b(?:how (?:many|long) (?:days|weeks|months|years)|how long|days? (?:passed|between|ago)|"
                r"weeks? ago|months? ago)\b",
    "order_when": r"\b(?:when|which .* first|first|before|after|earlier|later|most recent(?:ly)?|order)\b",
    "current_latest": r"\b(?:current(?:ly)?|now|latest|still|anymore|these days|most recent)\b",
    "date_anchor": r"\b(?:last (?:week|month|year|weekend|saturday|sunday|monday|tuesday|wednesday|thursday|friday)|"
                   r"yesterday|ago|in (?:january|february|march|april|may|june|july|august|september|october|"
                   r"november|december))\b",
}
RX = {k: re.compile(v, re.I) for k, v in CUES.items()}


def cues(q: str) -> set[str]:
    return {k for k, rx in RX.items() if rx.search(q)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--out", type=Path, default=Path("analysis/out/question_cues.json"))
    args = ap.parse_args()
    out = {}
    gold = {g["question_id"]: g for g in map(json.loads, open(args.root / "dev/gold.jsonl"))}
    by_type = defaultdict(Counter)
    n_type = Counter()
    for q in map(json.loads, open(args.root / "dev/questions.jsonl")):
        t = gold[q["question_id"]]["question_type"]
        n_type[t] += 1
        n_type["all"] += 1
        for c in cues(q["question"]):
            by_type[t][c] += 1
            by_type["all"][c] += 1
    out["dev"] = {t: {"n": n_type[t], **{c: round(by_type[t][c] / n_type[t], 3) for c in CUES}} for t in n_type}
    test = [json.loads(l) for l in open(args.root / "test/questions.jsonl")]
    tc = Counter(c for q in test for c in cues(q["question"]))
    out["test"] = {"n": len(test), **{c: round(tc[c] / len(test), 3) for c in CUES}}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1))
    cols = list(CUES)
    print("set/type".ljust(28) + "".join(c[:14].rjust(15) for c in ["n", *cols]))
    for t, row in sorted(out["dev"].items()):
        print(("dev " + t)[:28].ljust(28) + "".join(str(row[c]).rjust(15) for c in ["n", *cols]))
    print("test".ljust(28) + "".join(str(out["test"][c]).rjust(15) for c in ["n", *cols]))


if __name__ == "__main__":
    main()
