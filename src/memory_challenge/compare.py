"""Compare systems on the same questions with the organisers' own score.py: for example the kit's keyword baseline
(``reactor/examples/run_benchmark.py``, reproduced by ``configs/a-kit.toml``) against this framework.

Each system is a predictions file in the submission format and/or the payload directory it sent. Scores use
score.py's ``validate``, ``score_question`` and ``summarize`` unchanged, against a gold file holding exactly the
compared questions (a missing prediction counts as wrong, as in score.py). The first system is the reference; for
every other one the report gives per-type deltas, a per-question answer transition table (correct / unjudged / wrong /
abstained), evidence-F1 wins and losses, and paired bootstrap 95% intervals for the overall deltas. With payloads it
also compares what each system *sent*: gold sessions in the packed evidence, passages and message size, which needs no
Reactor run at all. Nothing here calls Reactor.
"""
from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

from .live import load_scorer

OUTCOMES = ("correct", "unjudged", "wrong", "abstained-ok", "abstained-wrong", "missing")


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def _outcome(row: dict, pred: dict | None) -> str:
    if pred is None:
        return "missing"
    if pred.get("abstain"):
        return "abstained-ok" if row["abstain_ok"] else "abstained-wrong"
    return row["answer"]


def score_system(predictions: Path, gold: dict[str, dict], scorer) -> list[dict]:
    """score.py's per-question rows for one predictions file, plus the outcome class used for transitions."""
    preds = _read_jsonl(predictions)
    errors = scorer.validate(preds, set(gold))
    if errors:
        raise ValueError(f"{predictions}: " + "; ".join(errors[:5]))
    by_id = {p["question_id"]: p for p in preds}
    rows = []
    for qid, g in gold.items():
        row = scorer.score_question(by_id.get(qid), g)
        row["outcome"] = _outcome(row, by_id.get(qid))
        rows.append(row)
    return rows


def retrieval_rows(payload_dir: Path, gold: dict[str, dict]) -> dict[str, dict]:
    """What a system sent for each compared question: gold sessions in the packed evidence, passages, characters."""
    d = Path(payload_dir)
    d = d / "payloads" if (d / "payloads").is_dir() else d
    out = {}
    for qid, g in gold.items():
        f = d / f"{qid}.json"
        if not f.exists():
            out[qid] = None
            continue
        p = json.loads(f.read_text(encoding="utf-8"))
        packed, ev = {e["id"] for e in p["evidence"]}, set(g["evidence"])
        out[qid] = {"passages": len(p["evidence"]), "message_chars": p.get("message_chars"),
                    "gold_recall": len(packed & ev) / len(ev) if ev else None,
                    "all_gold": float(ev <= packed) if ev else None}
    return out


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 4) if xs else None


def retrieval_summary(rows: dict[str, dict | None], gold: dict[str, dict]) -> dict[str, dict]:
    groups: dict[str, list[str]] = {}
    for qid, g in gold.items():
        groups.setdefault(g["question_type"], []).append(qid)
    groups = dict(sorted(groups.items()))
    groups["all"] = list(gold)
    table = {}
    for name, qids in groups.items():
        rs = [rows[q] for q in qids if rows.get(q)]
        table[name] = {"n": len(qids), "sent": len(rs), "gold_recall": _mean([r["gold_recall"] for r in rs]),
                       "all_gold": _mean([r["all_gold"] for r in rs]), "passages": _mean([r["passages"] for r in rs]),
                       "message_chars": _mean([r["message_chars"] for r in rs])}
    return table


def _bootstrap(deltas: list[float], n: int = 2000, seed: int = 0) -> list[float] | None:
    """95% paired bootstrap interval of the mean of per-question deltas."""
    if not deltas:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(deltas, k=len(deltas))) for _ in range(n))
    return [round(means[int(0.025 * n)], 4), round(means[int(0.975 * n) - 1], 4)]


def paired(ref: list[dict], other: list[dict]) -> dict:
    """Per-question comparison of two systems scored on the same gold (rows in the same order)."""
    transitions = {a: {b: 0 for b in OUTCOMES} for a in OUTCOMES}
    for r, o in zip(ref, other):
        transitions[r["outcome"]][o["outcome"]] += 1
    correct = [float(o["answer"] == "correct") - float(r["answer"] == "correct") for r, o in zip(ref, other)]
    f1 = [(o["evidence_f1"] - r["evidence_f1"]) for r, o in zip(ref, other)
          if r["evidence_f1"] is not None and o["evidence_f1"] is not None]
    return {"transitions": {a: {b: n for b, n in row.items() if n} for a, row in transitions.items() if any(row.values())},
            "correct": {"gained": sum(d > 0 for d in correct), "lost": sum(d < 0 for d in correct),
                        "delta": _mean(correct), "ci95": _bootstrap(correct)},
            "evidence_f1": {"better": sum(d > 0 for d in f1), "worse": sum(d < 0 for d in f1),
                            "same": sum(d == 0 for d in f1), "delta": _mean(f1), "ci95": _bootstrap(f1)}}


def compare(gold_path: Path, systems: dict[str, Path] | None = None, payloads: dict[str, Path] | None = None,
            reference: str | None = None) -> dict:
    """``systems``/``payloads`` map a label to a predictions file / payload directory. ``reference`` (default: the
    first label) is what the others are compared against."""
    systems, payloads = systems or {}, payloads or {}
    labels = list(dict.fromkeys([*systems, *payloads]))
    if reference:
        if reference not in labels:
            raise ValueError(f"reference {reference!r} is not one of the systems {labels}")
        labels.remove(reference)
        labels.insert(0, reference)
    if len(labels) < 2:
        raise ValueError("give at least two systems to compare")
    gold = {g["question_id"]: g for g in _read_jsonl(gold_path)}
    scorer = load_scorer()
    report: dict = {"gold": str(gold_path), "questions": len(gold), "reference": labels[0], "systems": labels,
                    "scores": {}, "paired": {}, "retrieval": {}, "per_question": []}
    scored = {lab: score_system(systems[lab], gold, scorer) for lab in labels if lab in systems}
    for lab, rows in scored.items():
        report["scores"][lab] = {t: {k: (round(v, 4) if isinstance(v, float) else v) for k, v in row.items()}
                                 for t, row in scorer.summarize(rows).items()}
    ref = labels[0]
    for lab in labels[1:]:
        if ref in scored and lab in scored:
            report["paired"][lab] = paired(scored[ref], scored[lab])
    sent = {lab: retrieval_rows(payloads[lab], gold) for lab in labels if lab in payloads}
    for lab, rows in sent.items():
        report["retrieval"][lab] = retrieval_summary(rows, gold)
    for i, (qid, g) in enumerate(gold.items()):
        row = {"question_id": qid, "type": g["question_type"], "gold_abstain": g["abstain"]}
        for lab in labels:
            if lab in scored:
                row[f"{lab}.outcome"] = scored[lab][i]["outcome"]
                row[f"{lab}.evidence_f1"] = scored[lab][i]["evidence_f1"]
            if lab in sent and sent[lab].get(qid):
                row[f"{lab}.gold_recall"] = sent[lab][qid]["gold_recall"]
        report["per_question"].append(row)
    return report


def _f(v) -> str:
    return "–" if v is None else f"{v:.3f}" if isinstance(v, float) else str(v)


def _delta(a, b) -> str:
    return "" if a is None or b is None or not isinstance(a, (int, float)) else f" ({b - a:+.3f})"


def markdown(report: dict) -> str:
    labels, ref = report["systems"], report["reference"]
    out = [f"# Comparison: {' vs '.join(labels)}", "",
           f"{report['questions']} questions, gold `{report['gold']}`. Reference: **{ref}**; deltas are other − {ref}. "
           "Scores use the organisers' score.py functions; `correct` counts only answers score.py matches itself "
           "(the official LLM judge decides `unjudged` ones), so it is a lower bound. Evidence F1 and abstention are "
           "exact."]
    if report["scores"]:
        cols = ("correct", "unjudged", "abstention_recall", "false_abstention", "evidence_f1")
        out += ["", "## Scores by question type (score.py)", "",
                "| type | n | " + " | ".join(f"{c} · {lab}" for c in cols for lab in labels if lab in report["scores"]) + " |",
                "|---|---|" + "---|" * sum(1 for _ in cols for lab in labels if lab in report["scores"])]
        first = next(iter(report["scores"].values()))
        types = [t for t in first if t != "all"] + ["all"]
        for t in types:
            cells = []
            for c in cols:
                base = report["scores"].get(ref, {}).get(t, {}).get(c)
                for lab in labels:
                    if lab in report["scores"]:
                        v = report["scores"][lab][t][c]
                        cells.append(_f(v) + ("" if lab == ref else _delta(base, v)))
            out.append(f"| {'**all**' if t == 'all' else t} | {first[t]['n']} | " + " | ".join(cells) + " |")
    for lab, p in report["paired"].items():
        c, e = p["correct"], p["evidence_f1"]
        out += ["", f"## {lab} against {ref}, question by question", "",
                f"- Answers judged correct by score.py: {c['gained']} gained, {c['lost']} lost; mean delta "
                f"{_f(c['delta'])}, 95% interval {c['ci95']}.",
                f"- Evidence F1 (answerable questions): better on {e['better']}, worse on {e['worse']}, same on "
                f"{e['same']}; mean delta {_f(e['delta'])}, 95% interval {e['ci95']}.",
                "", f"| {ref} → {lab} | " + " | ".join(OUTCOMES) + " |", "|---|" + "---|" * len(OUTCOMES)]
        for a in OUTCOMES:
            if a in p["transitions"]:
                out.append(f"| {a} | " + " | ".join(str(p["transitions"][a].get(b, "")) for b in OUTCOMES) + " |")
    if report["retrieval"]:
        out += ["", "## What each system sent (payloads; no Reactor needed)", "",
                "| type | " + " | ".join(f"{c} · {lab}" for c in ("gold recall", "all gold packed", "passages", "chars")
                                    for lab in labels if lab in report["retrieval"]) + " |",
                "|---|" + "---|" * (4 * len(report["retrieval"]))]
        types = list(next(iter(report["retrieval"].values())))
        types = [t for t in types if t != "all"] + ["all"]
        for t in types:
            cells = []
            for c in ("gold_recall", "all_gold", "passages", "message_chars"):
                base = report["retrieval"].get(ref, {}).get(t, {}).get(c)
                for lab in labels:
                    if lab in report["retrieval"]:
                        v = report["retrieval"][lab][t][c]
                        if c in ("passages", "message_chars"):
                            cells.append("–" if v is None else f"{v:.1f}" if c == "passages" else f"{v:,.0f}")
                        else:
                            cells.append(_f(v) + ("" if lab == ref else _delta(base, v)))
            out.append(f"| {'**all**' if t == 'all' else t} | " + " | ".join(cells) + " |")
    keys = [k for k in report["per_question"][0] if "." in k] if report["per_question"] else []
    if keys:
        out += ["", "## Per question", "", "| question | type | gold | " + " | ".join(keys) + " |",
                "|---|---|---|" + "---|" * len(keys)]
        for r in report["per_question"]:
            out.append(f"| `{r['question_id']}` | {r['type']} | {'unanswerable' if r['gold_abstain'] else 'answerable'} | "
                       + " | ".join(_f(r.get(k)) for k in keys) + " |")
    return "\n".join(out) + "\n"
