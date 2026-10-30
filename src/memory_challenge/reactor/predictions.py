"""Export and validate predictions.

Export runs the kit's own ``reactor/predictions.py`` (unmodified, as a subprocess) on ``results.jsonl``. Default
abstention policy: ``--abstain-on agent`` only, so Reactor's ``INSUFFICIENT_EVIDENCE`` becomes ``abstain: true``
while timeouts and errors are *not* turned into abstentions: they stay missing and are reported (a missing question
scores as wrong, as an answer would that the system never got). ``no-answer`` can be added deliberately.

With ``payloads``, answers that came back without usable ``[E#]`` labels get citations from the passages sent with
that question, only where the passage text supports the answer (``citation_fallback.py``, organiser-sanctioned).

Validation is stricter than the kit's: citations must be session ids of the question's own history (the kit checks
against every id in the file), ids must be known and unique, and every question must be present.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from ..data.loader import iter_history_lines, read_jsonl
from .citation_fallback import MAX_SESSIONS, MIN_SUPPORT, choose

SESSION_ID = re.compile(r'"session_id":\s*"([^"]+)"')


def session_index(histories: Path, out: Path) -> Path:
    """A histories file holding only ids: {"history_id", "sessions": [{"session_id"}]} per line.

    The kit's predictions.py reads --histories with ``str.splitlines()`` (reactor/predictions.py:167), which also
    splits on U+2028/U+2029 and other separators that occur inside conversation text in every released split (dev 97,
    dev-large 577, test 8 occurrences of U+2028), so it fails on the real files. It only needs the session ids, which
    this file carries without any conversation text."""
    with open(out, "w", encoding="utf-8") as f:
        for hid, line in iter_history_lines(histories):
            f.write(json.dumps({"history_id": hid, "sessions": [{"session_id": s} for s in SESSION_ID.findall(line)]}) + "\n")
    return out


def export(results: Path, histories: Path, out: Path, abstain_on: tuple[str, ...] = ("agent",),
           predictions_script: Path | None = None, payloads: Path | None = None,
           min_support: float = MIN_SUPPORT, max_sessions: int = MAX_SESSIONS) -> dict:
    """Kit conversion, then (with ``payloads``) the supported-citation fallback for answers that cite nothing.

    The kit's output is kept as ``<out>.kit.jsonl``; every fallback decision goes to ``<out>.citation_fallback.json``."""
    script = predictions_script or Path(__file__).resolve().parents[3] / "reactor" / "predictions.py"
    ids = session_index(histories, Path(str(out) + ".session_ids.jsonl"))
    cmd = [sys.executable, str(script), str(results), "--histories", str(ids), "--out", str(out)]
    if abstain_on:
        cmd += ["--abstain-on", ",".join(abstain_on)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    skipped = [l[len("skipped: "):] for l in proc.stderr.splitlines() if l.startswith("skipped: ")]
    notes = [l[len("note: "):] for l in proc.stderr.splitlines() if l.startswith("note: ")]
    report = {"exit_code": proc.returncode, "skipped": skipped, "notes": notes,
              "summary": (proc.stderr.strip().splitlines() or [""])[-1], "abstain_on": list(abstain_on)}
    if payloads is not None and proc.returncode in (0, 1) and Path(out).exists():
        report["citation_fallback"] = fill_citations(Path(out), Path(results), histories, Path(payloads),
                                                     min_support, max_sessions)
    return report


def fill_citations(predictions: Path, results: Path, histories: Path, payload_dir: Path,
                   min_support: float = MIN_SUPPORT, max_sessions: int = MAX_SESSIONS) -> dict:
    """Apply ``citation_fallback.choose`` to every answered prediction that cites nothing; rewrite ``predictions``."""
    preds = list(read_jsonl(predictions))
    why_empty = {}
    for r in read_jsonl(results):
        if r.get("status") == "answered" and not r.get("abstained"):
            why_empty[r.get("question_id")] = ("no [E#] label in the final answer" if r.get("uncited") else
                                               f"labels never sent: {r.get('unknown_citations')}" if r.get("unknown_citations")
                                               else "cited passages without a session id" if r.get("citations") else "no citation")
    payloads = {}
    for p in preds:
        f = payload_dir / f"{p['question_id']}.json"
        if not p["abstain"] and not p["citations"] and f.exists():
            payloads[p["question_id"]] = json.loads(f.read_text(encoding="utf-8"))
    own = history_sessions(histories, {pl["dataset_history_id"] for pl in payloads.values()})
    decisions, filled = [], 0
    for p in preds:
        if p["abstain"] or p["citations"]:
            continue
        pl = payloads.get(p["question_id"])
        if pl is None:
            decisions.append({"question_id": p["question_id"], "filled": [], "reason": "no payload found: left empty"})
            continue
        chosen, rec = choose(p["answer"], pl, own.get(pl["dataset_history_id"], set()), min_support, max_sessions)
        p["citations"] = chosen  # the answer text is never touched
        filled += bool(chosen)
        decisions.append({"question_id": p["question_id"], "why_no_citation": why_empty.get(p["question_id"]), **rec})
    kit_copy = Path(str(predictions) + ".kit.jsonl")
    kit_copy.write_text(predictions.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = Path(str(predictions) + ".tmp")
    tmp.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in preds), encoding="utf-8")
    tmp.replace(predictions)
    report = {"policy": "cite packed passages whose quoted text supports the answer; never the top-ranked by default",
              "min_support": min_support, "max_sessions": max_sessions, "candidates": len(decisions),
              "filled": filled, "left_empty": len(decisions) - filled, "decisions": decisions,
              "kit_output": str(kit_copy)}
    Path(str(predictions) + ".citation_fallback.json").write_text(json.dumps(report, indent=1, ensure_ascii=False),
                                                                    encoding="utf-8")
    return {k: v for k, v in report.items() if k != "decisions"}


def history_sessions(histories: Path, wanted: set[str]) -> dict[str, set[str]]:
    return {hid: set(SESSION_ID.findall(line)) for hid, line in iter_history_lines(histories, wanted)}


def validate(predictions: Path, questions: Path, histories: Path) -> dict:
    qs = {q["question_id"]: q for q in read_jsonl(questions)}
    errors, warnings = [], []
    preds, seen = [], set()
    for n, p in enumerate(read_jsonl(predictions), 1):
        where = f"line {n}"
        for k, kind in (("question_id", str), ("abstain", bool), ("answer", str), ("citations", list)):
            if not isinstance(p.get(k), kind):
                errors.append(f"{where}: {k} missing or not {kind.__name__}")
        qid = p.get("question_id")
        if qid in seen:
            errors.append(f"{where}: duplicate question_id {qid}")
        elif qid not in qs:
            errors.append(f"{where}: unknown question_id {qid}")
        seen.add(qid)
        preds.append(p)
    sessions = history_sessions(histories, {q["history_id"] for q in qs.values()})
    for p in preds:
        q = qs.get(p.get("question_id"))
        if not q or not isinstance(p.get("citations"), list):
            continue
        own = sessions.get(q["history_id"], set())
        bad = [c for c in p["citations"] if c not in own]
        if bad:
            errors.append(f"{p['question_id']}: citations not in its own history {q['history_id']}: {bad}")
        if p.get("abstain") and p.get("citations"):
            warnings.append(f"{p['question_id']}: abstains but cites sessions")
    missing = sorted(set(qs) - seen)
    if missing:
        warnings.append(f"{len(missing)} question(s) without a prediction (scored as wrong): {missing[:10]}")
    return {"predictions": len(preds), "questions": len(qs), "missing": len(missing), "errors": errors,
            "warnings": warnings, "valid": not errors}


def official_score(gold: Path, predictions: Path, details: Path | None = None) -> str:
    script = Path(__file__).resolve().parents[3] / "score.py"
    cmd = [sys.executable, str(script), str(gold), str(predictions)] + (["--details", str(details)] if details else [])
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    return (proc.stdout + proc.stderr).strip()
