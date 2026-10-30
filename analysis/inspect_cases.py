"""Pick a small, deliberately varied set of dev cases and write short excerpts of them (diagnostics only).

Conversation text is untrusted data: excerpts are quoted, cut to EXCERPT characters, never interpreted.
Selection is deterministic (first match in file order for each criterion).

    python -I analysis/inspect_cases.py --split dev --out analysis/out/cases.md
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

EXCERPT = 260
REL = re.compile(r"\b(?:yesterday|today|last (?:week|weekend|month|year|monday|tuesday|wednesday|thursday|friday|"
                 r"saturday|sunday)|(?:\d+|a|two|three|four|five|a few|a couple of) (?:day|week|month)s? ago)\b", re.I)


def norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+(?:[.:/][0-9]+)*", text.lower()))


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", text.lower()))


def cut(text: str, around: int | None = None) -> str:
    text = " ".join(text.split())
    if around is None or len(text) <= EXCERPT:
        return text[:EXCERPT] + ("…" if len(text) > EXCERPT else "")
    start = max(0, min(around - EXCERPT // 3, len(text) - EXCERPT))
    return ("…" if start else "") + text[start:start + EXCERPT] + "…"


def best_turn(session: dict, question: str, answer: str | None = None, role: str | None = None):
    """The turn holding the answer string if any, else the one sharing most 4+ letter words with the question
    (among ``role``'s turns when given and present)."""
    if answer:
        for t in session["turns"]:
            i = " ".join(t["content"].split()).lower().find(answer.lower())
            if i >= 0:
                return t, i
    qw = words(question)
    turns = [t for t in session["turns"] if t["role"] == role] or session["turns"] if role else session["turns"]
    t = max(turns, key=lambda t: len(qw & words(t["content"])))
    return t, None


def fmt_turn(session: dict, turn: dict, at: int | None) -> str:
    return f"  - `{session['session_id']}` {session['date']} **{turn['role']}**: \"{cut(turn['content'], at)}\""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--split", default="dev")
    ap.add_argument("--out", type=Path, default=Path("analysis/out/cases.md"))
    args = ap.parse_args()
    d = args.root / args.split
    qs = [json.loads(l) for l in open(d / "questions.jsonl")]
    gold = {g["question_id"]: g for g in map(json.loads, open(d / "gold.jsonl"))}
    by_h = {}
    for q in qs:
        by_h.setdefault(q["history_id"], []).append(q)
    wanted = {"single-session-user", "single-session-assistant", "multi-session", "knowledge-update",
              "temporal-reasoning", "abstention", "near-miss", "short-reply", "uncued-pair"}
    picked = {}
    for line in open(d / "histories.jsonl"):
        if not wanted - set(picked):
            break
        h = json.loads(line)
        sessions = {s["session_id"]: s for s in h["sessions"]}
        for q in by_h[h["history_id"]]:
            g = gold[q["question_id"]]
            t = g["question_type"]
            ev = [sessions[e] for e in g["evidence"]]
            ans = g["answer"] if 3 <= len(g["answer"]) <= 60 else None
            if g["abstain"] and "abstention" not in picked:
                qw = words(q["question"])
                near = max(h["sessions"], key=lambda s: max(len(qw & words(x["content"])) for x in s["turns"]))
                picked["abstention"] = (q, g, [fmt_turn(near, *best_turn(near, q["question"]))],
                                        "closest session by word overlap (not evidence)")
                continue
            if g["abstain"]:
                continue
            if len(by_h[h["history_id"]]) == 2 and "uncued-pair" not in picked:
                other = [x for x in by_h[h["history_id"]] if x is not q][0]
                picked["uncued-pair"] = (q, g, [f"  - paired question `{other['question_id']}`: \"{other['question']}\""
                                                f" (gold answer identical: {gold[other['question_id']]['answer'] == g['answer']})"],
                                         "two questions, one history, same gold")
            if t in wanted and t not in picked:
                if t == "multi-session" and len(ev) < 3:
                    continue
                if t == "temporal-reasoning" and not any(REL.search(x["content"]) for s in ev for x in s["turns"]):
                    continue
                if t == "single-session-user" and not (ans and any(norm(ans) in norm(x["content"]) for s in ev for x in s["turns"] if x["role"] == "user")):
                    continue
                lines = []
                for s in sorted(ev, key=lambda s: s["date"]):
                    if t == "temporal-reasoning":
                        hit = next(((x, m.start()) for x in s["turns"] if x["role"] == "user"
                                    for m in [REL.search(" ".join(x["content"].split()))] if m), None)
                        lines.append(fmt_turn(s, *(hit or best_turn(s, q["question"], role="user"))))
                    elif t == "multi-session":
                        lines.append(fmt_turn(s, *best_turn(s, q["question"], ans, role="user")))
                    else:
                        lines.append(fmt_turn(s, *best_turn(s, q["question"], ans)))
                picked[t] = (q, g, lines, f"{len(ev)} evidence session(s)")
            if ans and "near-miss" not in picked:
                elsewhere = [s for s in h["sessions"] if s["session_id"] not in g["evidence"]
                             and any(f" {norm(ans)} " in f" {norm(x['content'])} " for x in s["turns"])]
                if elsewhere and len(norm(ans)) > 4:
                    lines = [fmt_turn(s, *best_turn(s, q["question"], ans)) + " (evidence)" for s in ev[:1]]
                    lines += [fmt_turn(s, *best_turn(s, q["question"], ans)) + " (NOT evidence)" for s in elsewhere[:1]]
                    picked["near-miss"] = (q, g, lines, f"answer string also in {len(elsewhere)} non-evidence session(s)")
            if "short-reply" not in picked:
                for s in ev:
                    for a, b in zip(s["turns"], s["turns"][1:]):
                        if a["role"] == "assistant" and b["role"] == "user" and 0 < len(b["content"].strip()) < 40:
                            picked["short-reply"] = (q, g, [fmt_turn(s, a, len(" ".join(a["content"].split()))),
                                                            fmt_turn(s, b, None)],
                                                     "short user reply inside an evidence session")
                            break
                    if "short-reply" in picked:
                        break
    out = [f"# Varied cases from `{args.split}` (diagnostic excerpts, ≤{EXCERPT} chars each)", ""]
    for name in ["single-session-user", "single-session-assistant", "uncued-pair", "multi-session",
                 "knowledge-update", "temporal-reasoning", "abstention", "near-miss", "short-reply"]:
        if name not in picked:
            out += [f"## {name}", "", "_no case matched the criterion_", ""]
            continue
        q, g, lines, note = picked[name]
        out += [f"## {name}", "",
                f"- question `{q['question_id']}` ({g['question_type']}), asked {q['question_date']}: \"{q['question']}\"",
                f"- gold: abstain={g['abstain']}, answer: \"{cut(g['answer'])}\"; {note}", *lines, ""]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {args.out}: {len(picked)} cases")


if __name__ == "__main__":
    main()
