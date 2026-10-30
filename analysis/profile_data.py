"""Profile the challenge splits: structure, integrity, sizes, content markers, and (dev splits) gold evidence.

Exploration only: reads the released files, imports no repository code, sends nothing anywhere. Gold is used for
diagnostics, never for anything that feeds inference. Histories are streamed one line (one history) at a time.

    python -I analysis/profile_data.py --root . --out analysis/out/data_profile.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

DATE_FMT = "%Y/%m/%d (%a) %H:%M"
PCTS = (50, 90, 95, 99)

_DAYS = r"monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
           r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_NUM = r"\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|a few|a couple of|couple of|several"
MARKERS = {
    # Heuristic lexical markers: counts are indicative only, not labels.
    "relative_date": re.compile(
        rf"\b(?:yesterday|today|tonight|tomorrow|"
        rf"(?:last|this|next|past|coming)\s+(?:week(?:end)?|month|year|night|summer|winter|spring|fall|autumn|{_DAYS})|"
        rf"(?:{_NUM})\s+(?:day|week|month|year)s?\s+(?:ago|later|before|after|from now|back)|"
        rf"(?:on|since)\s+(?:{_DAYS}))\b", re.I),
    "explicit_date": re.compile(
        rf"\b(?:(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\b|"
        rf"\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}})", re.I),
    "update_marker": re.compile(
        r"\b(?:actually|update[ds]?|changed my mind|no longer|not anymore|any ?more|switched to|instead of|"
        r"turns out|correction|i meant|now i(?:'m| am| have|'ve)?|used to)\b", re.I),
}
SHORT_REPLY = 40  # characters: a user turn this short often needs the previous turn to make sense


def read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def pct(values) -> dict | None:
    if not len(values):
        return None
    a = np.asarray(values, dtype=float)
    out = {"n": int(a.size), "mean": round(float(a.mean()), 1)}
    out.update({f"p{p}": round(float(np.percentile(a, p)), 1) for p in PCTS})
    out.update({"min": round(float(a.min()), 1), "max": round(float(a.max()), 1)})
    return out


def parse_date(s: str):
    """(datetime, weekday_ok) or (None, False) for an unparseable date."""
    try:
        d = datetime.strptime(s, DATE_FMT)
    except (TypeError, ValueError):
        return None, False
    return d, True


def norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+(?:[.:/][0-9]+)*", text.lower()))


def session_hash(turns) -> str:
    h = hashlib.sha1()
    for t in turns:
        h.update(t.get("role", "").encode())
        h.update(b"\0")
        h.update(t.get("content", "").encode())
        h.update(b"\1")
    return h.hexdigest()


def message_overhead(question: str, n_passages: int, question_date_with_time: bool = True) -> int:
    """Characters client.py's build_message (reactor/client.py:218-246) adds around the question and passage texts.
    Mirrors its template string by string; the ref is always 8 hex characters; ids are 14 and dates 22 characters."""
    ref = "x" * 8
    lines = [f"[ref:{ref}] Question: {question}"]
    if question_date_with_time:
        lines.append("Question date: 2023-05-30 (Tue) 08:08, no timezone. Answer as of that moment: treat it as now "
                     "for this question; the clock you see shows the real date and time -- ignore it here.")
    if n_passages:
        lines += ["", "Evidence:"]
        for n in range(1, n_passages + 1):
            lines.append(f"[E{n}] (source: {'s_' + 'x' * 12}, date: {'2023/05/20 (Sat) 02:21'}) ")
        lines += ["", "Answer from the evidence above and cite each passage you use by its label, e.g. [E1]."]
    lines += ["", "When you have your answer, write it on a line of its own that starts exactly with:",
              f"# FINAL ANSWER [ref:{ref}]: ",
              f"If the information you have doesn't answer the question, that line is: # FINAL ANSWER [ref:{ref}]: "
              "INSUFFICIENT_EVIDENCE",
              "Anything you send before that line is an interim note, not the answer."]
    return len("\n".join(lines)) - len(question)


class SplitProfile:
    def __init__(self, root: Path, split: str, content_seen: set):
        self.root, self.split = root, split
        self.content_seen = content_seen  # session hashes whose content markers were already counted (any split)
        self.questions = list(read_jsonl(root / split / "questions.jsonl"))
        gold_path = root / split / "gold.jsonl"
        self.gold = {g["question_id"]: g for g in read_jsonl(gold_path)} if gold_path.exists() else None
        self.q_by_h = defaultdict(list)
        for q in self.questions:
            self.q_by_h[q["history_id"]].append(q)
        self.issues = Counter()
        self.c = Counter()
        self.sizes = defaultdict(list)
        self.session_occ = defaultdict(list)  # sid -> [(hid, date_str)]
        self.session_hash = {}  # sid -> content hash
        self.hash_ids = defaultdict(set)
        self.history_sessions = {}  # hid -> set(sid)
        self.history_dates = {}  # hid -> {sid: date_str}
        self.markers = {k: Counter() for k in MARKERS}
        self.marker_sessions = {k: Counter() for k in MARKERS}
        self.gold_rows = []
        self.long_turn_examples = []

    def run(self):
        hist_ids = Counter()
        for h in read_jsonl(self.root / self.split / "histories.jsonl"):
            self.history(h)
            hist_ids[h.get("history_id")] += 1
        self.c["histories"] = len(hist_ids)
        self.issues["duplicate_history_id"] = sum(v - 1 for v in hist_ids.values() if v > 1)
        missing = set(self.q_by_h) - set(hist_ids)
        self.issues["questions_without_history"] = sum(len(self.q_by_h[h]) for h in missing)
        self.issues["histories_without_question"] = len(set(hist_ids) - set(self.q_by_h))
        qids = Counter(q["question_id"] for q in self.questions)
        self.issues["duplicate_question_id"] = sum(v - 1 for v in qids.values() if v > 1)
        for q in self.questions:
            for k, t in (("question_id", str), ("history_id", str), ("question", str), ("question_date", str)):
                if not isinstance(q.get(k), t):
                    self.issues[f"question_bad_{k}"] += 1
            if parse_date(q.get("question_date"))[0] is None:
                self.issues["question_date_unparseable"] += 1
            self.sizes["question_chars"].append(len(q.get("question", "")))
        if self.gold is not None:
            self.issues["gold_without_question"] = len(set(self.gold) - set(qids))
            self.issues["question_without_gold"] = len(set(qids) - set(self.gold))

    def history(self, h):
        hid = h.get("history_id")
        sessions = h.get("sessions")
        if not isinstance(hid, str) or not isinstance(sessions, list):
            self.issues["history_bad_fields"] += 1
            return
        sids, dates, prev, hist_chars, turn_ids = set(), {}, None, 0, Counter()
        for s in sessions:
            sid, date, turns = s.get("session_id"), s.get("date"), s.get("turns")
            if not isinstance(sid, str) or not isinstance(date, str) or not isinstance(turns, list):
                self.issues["session_bad_fields"] += 1
                continue
            if sid in sids:
                self.issues["duplicate_session_in_history"] += 1
            sids.add(sid)
            dates[sid] = date
            d, ok = parse_date(date)
            if d is None:
                self.issues["session_date_unparseable"] += 1
            elif prev is not None and d < prev:
                self.issues["sessions_out_of_order"] += 1
            prev = d or prev
            hsh = session_hash(turns)
            if sid in self.session_hash and self.session_hash[sid] != hsh:
                self.issues["same_session_id_different_content"] += 1
            self.session_hash[sid] = hsh
            self.hash_ids[hsh].add(sid)
            self.session_occ[sid].append((hid, date))
            s_chars, roles = 0, []
            if not turns:
                self.issues["empty_session"] += 1
            for i, t in enumerate(turns):
                role, content, tid = t.get("role"), t.get("content"), t.get("turn_id")
                if role not in ("user", "assistant"):
                    self.issues["turn_bad_role"] += 1
                if not isinstance(content, str):
                    self.issues["turn_bad_content"] += 1
                    continue
                if not isinstance(tid, str):
                    self.issues["turn_bad_id"] += 1
                turn_ids[tid] += 1
                if not content.strip():
                    self.c[f"empty_turns_{role}"] += 1
                n = len(content)
                self.sizes[f"turn_chars_{role}"].append(n)
                self.c[f"turns_{role}"] += 1
                self.c[f"chars_{role}"] += n
                s_chars += n
                roles.append(role)
                if role == "user" and 0 < len(content.strip()) < SHORT_REPLY and i > 0:
                    self.c["short_user_followups"] += 1
                if n > 16000 and len(self.long_turn_examples) < 5:
                    self.long_turn_examples.append({"session_id": sid, "turn_index": i, "role": role, "chars": n})
                for thr in (2000, 5000, 10000, 16000, 32000):
                    if n > thr:
                        self.c[f"turns_over_{thr}"] += 1
            if roles and roles[0] != "user":
                self.c["sessions_starting_with_assistant"] += 1
            self.c["role_alternation_breaks"] += sum(a == b for a, b in zip(roles, roles[1:]))
            self.sizes["session_chars"].append(s_chars)
            self.sizes["session_turns"].append(len(turns))
            for thr in (10000, 16000, 32000):
                if s_chars > thr:
                    self.c[f"sessions_over_{thr}"] += 1
            hist_chars += s_chars
            self.c["sessions"] += 1
            if hsh not in self.content_seen:
                self.content_seen.add(hsh)
                self.c["distinct_sessions_marker_scanned"] += 1
                self.scan_markers(turns)
        self.issues["duplicate_turn_id_in_history"] += sum(v - 1 for v in turn_ids.values() if v > 1)
        self.history_sessions[hid] = sids
        self.history_dates[hid] = dates
        self.sizes["history_sessions"].append(len(sessions))
        self.sizes["history_chars"].append(hist_chars)
        self.sizes["questions_per_history"].append(len(self.q_by_h.get(hid, [])))
        for q in self.q_by_h.get(hid, []):
            self.question_vs_history(q, sessions, dates)

    def scan_markers(self, turns):
        found = Counter()
        for t in turns:
            content = t.get("content") or ""
            for name, rx in MARKERS.items():
                n = len(rx.findall(content))
                if n:
                    self.markers[name][t.get("role")] += n
                    found[(name, t.get("role"))] += 1
        for (name, role) in found:
            self.marker_sessions[name][role] += 1

    def question_vs_history(self, q, sessions, dates):
        qd, _ = parse_date(q.get("question_date"))
        parsed = [parse_date(s.get("date"))[0] for s in sessions]
        if qd and any(d and d > qd for d in parsed):
            self.issues["session_after_question"] += 1
        if qd and parsed and parsed[-1]:
            self.sizes["days_last_session_to_question"].append((qd - parsed[-1]).total_seconds() / 86400)
        if qd and parsed and parsed[0]:
            self.sizes["history_span_days"].append((qd - parsed[0]).total_seconds() / 86400)
        if self.gold is None or q["question_id"] not in self.gold:
            return
        g = self.gold[q["question_id"]]
        index = {s["session_id"]: i for i, s in enumerate(sessions)}
        n = len(sessions)
        row = {"question_id": q["question_id"], "history_id": q["history_id"], "type": g["question_type"],
               "abstain": g["abstain"], "n_evidence": len(g["evidence"]), "n_sessions": n,
               "evidence_missing": [e for e in g["evidence"] if e not in index],
               "positions": [], "days_before_question": [], "evidence_chars": 0, "answer_roles": Counter(),
               "answer_turn_pos": [], "answer_in_other_sessions": None, "evidence_has_relative_date": False,
               "question_chars": len(q["question"])}
        ans = norm(g["answer"]) if not g["abstain"] else ""
        matchable = 3 <= len(ans) <= 60 and not re.fullmatch(r"\d{1,2}", ans)
        other_hits = 0
        for i, s in enumerate(sessions):
            is_ev = s["session_id"] in g["evidence"]
            if is_ev:
                row["positions"].append(i / max(n - 1, 1))
                d = parsed[i]
                if qd and d:
                    row["days_before_question"].append((qd - d).total_seconds() / 86400)
                row["evidence_chars"] += sum(len(t["content"]) for t in s["turns"])
                if any(MARKERS["relative_date"].search(t["content"]) for t in s["turns"] if t["role"] == "user"):
                    row["evidence_has_relative_date"] = True
            if not matchable:
                continue
            hit_here = False
            for j, t in enumerate(s["turns"]):
                if f" {ans} " in f" {norm(t['content'])} ":
                    hit_here = True
                    if is_ev:
                        row["answer_roles"][t["role"]] += 1
                        row["answer_turn_pos"].append(j / max(len(s["turns"]) - 1, 1))
            if hit_here and not is_ev:
                other_hits += 1
        if matchable:
            row["answer_in_other_sessions"] = other_hits
        row["answer_matchable"] = matchable
        row["answer_roles"] = dict(row["answer_roles"])
        self.gold_rows.append(row)

    def report(self) -> dict:
        reused = {sid: occ for sid, occ in self.session_occ.items() if len(occ) > 1}
        date_spreads = []
        for occ in reused.values():
            ds = [parse_date(d)[0] for _, d in occ]
            ds = [d for d in ds if d]
            if len(ds) > 1:
                date_spreads.append((max(ds) - min(ds)).total_seconds() / 86400)
        hist_per_session = [len(o) for o in self.session_occ.values()]
        out = {
            "split": self.split,
            "counts": {"histories": self.c["histories"], "sessions_total": self.c["sessions"],
                       "distinct_session_ids": len(self.session_occ),
                       "distinct_session_contents": len(self.hash_ids),
                       "contents_with_several_ids": sum(len(v) > 1 for v in self.hash_ids.values()),
                       "questions": len(self.questions),
                       "turns_user": self.c["turns_user"], "turns_assistant": self.c["turns_assistant"],
                       "chars_user": self.c["chars_user"], "chars_assistant": self.c["chars_assistant"]},
            "integrity_issues": {k: v for k, v in sorted(self.issues.items())},
            "session_reuse": {"sessions_in_more_than_one_history": len(reused),
                              "histories_per_session": pct(hist_per_session),
                              "date_spread_days_for_reused": pct(date_spreads),
                              "reused_with_different_dates": sum(d > 0 for d in date_spreads)},
            "sizes": {k: pct(v) for k, v in sorted(self.sizes.items())},
            "other_counts": {k: v for k, v in sorted(self.c.items()) if k not in ("histories", "sessions")},
            "markers_per_role": {k: dict(v) for k, v in self.markers.items()},
            "sessions_with_marker_per_role": {k: dict(v) for k, v in self.marker_sessions.items()},
            "long_turn_examples": self.long_turn_examples,
            "message_overhead_chars": {"no_passages": message_overhead("", 0), "per_passage_header":
                                       message_overhead("", 2) - message_overhead("", 1),
                                       "with_one_passage_excl_text": message_overhead("", 1)},
        }
        if self.gold_rows:
            out["gold"] = self.gold_report()
        return out

    def gold_report(self) -> dict:
        rows = self.gold_rows
        by_type = defaultdict(list)
        for r in rows:
            by_type[r["type"]].append(r)
        res = {"missing_evidence_ids": sum(len(r["evidence_missing"]) for r in rows),
               "uncued_variant_histories": sum(len(v) > 1 for v in self.q_by_h.values()), "types": {}}
        for t, rs in sorted(by_type.items()):
            ans = [r for r in rs if not r["abstain"]]
            matchable = [r for r in ans if r["answer_matchable"]]
            found = [r for r in matchable if r["answer_roles"]]
            role_mix = Counter()
            for r in found:
                roles = set(r["answer_roles"])
                role_mix["both" if len(roles) > 1 else next(iter(roles))] += 1
            res["types"][t] = {
                "n": len(rs), "abstain": sum(r["abstain"] for r in rs),
                "evidence_sessions": dict(sorted(Counter(r["n_evidence"] for r in ans).items())),
                "evidence_position": pct([p for r in ans for p in r["positions"]]),
                "evidence_days_before_question": pct([d for r in ans for d in r["days_before_question"]]),
                "evidence_chars_total": pct([r["evidence_chars"] for r in ans]),
                "evidence_total_over_16k": sum(r["evidence_chars"] > 16000 for r in ans),
                "evidence_total_over_32k": sum(r["evidence_chars"] > 32000 for r in ans),
                "answer_string_matchable": len(matchable),
                "answer_found_in_evidence": len(found),
                "answer_found_role": dict(role_mix),
                "answer_turn_position_in_session": pct([p for r in found for p in r["answer_turn_pos"]]),
                "answer_also_in_n_other_sessions": pct([r["answer_in_other_sessions"] for r in matchable]),
                "matchable_answers_also_elsewhere": sum((r["answer_in_other_sessions"] or 0) > 0 for r in matchable),
                "evidence_with_user_relative_date": sum(r["evidence_has_relative_date"] for r in ans),
                "question_chars": pct([r["question_chars"] for r in rs]),
            }
        return res


def compare(a: SplitProfile, b: SplitProfile) -> dict:
    """How two splits relate: shared history ids, session subset relations, date changes on shared sessions."""
    shared_h = set(a.history_sessions) & set(b.history_sessions)
    subset = sum(a.history_sessions[h] <= b.history_sessions[h] for h in shared_h)
    date_changed = same_sessions = 0
    for h in shared_h:
        for sid in a.history_sessions[h] & b.history_sessions[h]:
            same_sessions += 1
            date_changed += a.history_dates[h][sid] != b.history_dates[h][sid]
    sid_overlap = set(a.session_occ) & set(b.session_occ)
    hash_overlap = set(a.hash_ids) & set(b.hash_ids)
    qa = {q["question"] for q in a.questions}
    qb = {q["question"] for q in b.questions}
    return {"shared_history_ids": len(shared_h), f"{a.split}_history_subset_of_{b.split}": subset,
            "shared_sessions_within_same_history": same_sessions,
            "shared_sessions_with_changed_date": date_changed,
            "shared_session_ids_any_history": len(sid_overlap),
            "shared_session_contents_any_history": len(hash_overlap),
            "identical_question_texts": len(qa & qb)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--splits", nargs="+", default=["dev", "dev-large", "test"])
    ap.add_argument("--out", type=Path, default=Path("analysis/out/data_profile.json"))
    args = ap.parse_args()
    content_seen: set = set()
    profiles, report = {}, {"splits": {}, "comparisons": {}, "timing_s": {}}
    for split in args.splits:
        if not (args.root / split / "histories.jsonl").exists():
            print(f"{split}: not present, skipped", file=sys.stderr)
            continue
        t0 = time.time()
        p = SplitProfile(args.root, split, content_seen)
        p.run()
        profiles[split] = p
        report["splits"][split] = p.report()
        report["timing_s"][split] = round(time.time() - t0, 1)
        print(f"{split}: profiled in {report['timing_s'][split]}s", file=sys.stderr)
    names = list(profiles)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            report["comparisons"][f"{a}__{b}"] = compare(profiles[a], profiles[b])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
