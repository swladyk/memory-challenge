"""Drive the team's pipeline (dashboard/evaluation.py + submission/workflow.py) with this repo's harness, offline.

No Reactor call is ever made: answers come from a deterministic fake transport (or none, with --dry-run). What runs
for real: our `memret harness` server over HTTP, the dashboard's harness client and message compiler
(`evaluation.prepare`, `compile_message`, `execute`), the workflow's checkpoint format and strict
`export_predictions`, and (dev) the official `score.py` on the same question subset.

    PYTHONPATH=src .venv/bin/python tools/team_pipeline_offline.py --split dev --limit 20 --out runs/team-offline-dev
    PYTHONPATH=src .venv/bin/python tools/team_pipeline_offline.py --split test --dry-run --out runs/team-offline-test

The fake transport cites its best-matching passage; every --no-label-every'th answer is sent without an [E#] label so
the citation fallback is exercised. Fake answers say nothing about answer quality.
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "dashboard", ROOT / "submission"):
    sys.path.insert(0, str(p))

import evaluation  # noqa: E402  (the team's dashboard module)
import workflow  # noqa: E402  (the team's submission workflow)
from memory_challenge.config import load_config  # noqa: E402
from memory_challenge.harness import serve  # noqa: E402


def fake_transport(no_label: set[str]):
    def transport(message, evidence, ref, history_id, access_key):
        q = re.search(r"Question: (.*)", message["text"]).group(1)
        qid = transport.current
        if not evidence:
            final = "INSUFFICIENT_EVIDENCE"
        else:
            qw = set(re.findall(r"[a-z]{4,}", q.lower()))
            best = max(range(len(evidence)), key=lambda i: len(qw & set(re.findall(r"[a-z]{4,}", evidence[i]["text"].lower()))))
            first = evidence[best]["text"].split("\n")[0].split(": ", 1)[-1][:120].strip()
            final = first if qid in no_label else f"{first} [E{best + 1}]"
        reply = f"# FINAL ANSWER [ref:{ref}]: {final}"
        cited, unknown = evaluation.client.citations(final, evidence)
        return {"status": "answered", "answer": final, "reply": reply, "cycle": 1, "latency_s": 0.0,
                "other_replies": [], "late_replies": [], "retries": 0, "citations": cited, "unknown_citations": unknown,
                "uncited": bool(evidence) and not cited, "abstained": final.startswith("INSUFFICIENT_EVIDENCE")}
    transport.current = None
    return transport


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", required=True)
    ap.add_argument("--config")
    ap.add_argument("--limit", type=int, help="evenly spaced subset of this many questions")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true", help="prepare and compile only (the dashboard's dry_run)")
    ap.add_argument("--no-label-every", type=int, default=3)
    ap.add_argument("--citation-fallback", action="store_true")
    args = ap.parse_args()
    split, out = Path(args.split), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    questions = workflow.read_jsonl(split / "questions.jsonl")
    if args.limit and args.limit < len(questions):
        step = len(questions) / args.limit
        questions = [questions[int(i * step)] for i in range(args.limit)]
    qfile = out / "questions.subset.jsonl"
    qfile.write_text("".join(json.dumps(q) + "\n" for q in questions), encoding="utf-8")
    cfg = load_config(args.config)
    server = serve(split, cfg, "127.0.0.1", 0, out / "harness")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = evaluation.validate_harness({"enabled": True, "url": f"http://127.0.0.1:{server.server_port}/prepare",
                                          "timeout_s": 120, "token": ""})
    _, histories = workflow.dataset(qfile, split / "histories.jsonl")
    no_label = {q["question_id"] for i, q in enumerate(questions) if args.no_label_every and i % args.no_label_every == 0}
    transport = fake_transport(no_label)
    groups: dict[str, list[dict]] = {}
    for q in questions:
        groups.setdefault(q["history_id"], []).append(q)
    checkpoint = {"fingerprint": {"questions_sha256": workflow.digest(qfile),
                                  "histories_sha256": workflow.digest(split / "histories.jsonl"),
                                  "evidence_mode": "none", "harness": {k: v for k, v in config.items() if k != "token"},
                                  "harness_version": "memory_challenge " + cfg.hash(), "offline": True},
                  "completed_histories": {}, "run_ids": {}}
    checks = {"runs": 0, "prepared": 0, "completed": 0, "failed": 0, "max_message_chars": 0, "foreign_evidence": 0,
              "wrong_dates": 0, "errors": []}
    for n, (hid, group) in enumerate(groups.items()):
        reactor_history = f"offline-{n:03d}"
        sessions = {s["session_id"]: s["date"] for s in histories[hid]["sessions"]}
        for q in group:
            source = evaluation.validate_input({"original_prompt": q["question"], "question_id": q["question_id"],
                                                "question_date": q["question_date"], "evidence": [],
                                                "context": {"dataset_history_id": hid},
                                                "history_id": reactor_history, "dry_run": args.dry_run})
            source["history_id"] = source.get("history_id") or reactor_history
            run = {"run_id": "run_" + secrets.token_hex(16), "schema_version": "halo.run.v1", "status": "queued",
                   "input": source, "harness": {k: v for k, v in config.items() if k != "token"}, "stages": []}
            transport.current = q["question_id"]
            evaluation.execute(run, source, config, "", lambda r: None, transport=transport)
            workflow.atomic_json(out / "runs" / f"{run['run_id']}.json", run)
            checks["runs"] += 1
            checks[run["status"]] = checks.get(run["status"], 0) + 1
            if run["status"] == "failed":
                checks["errors"].append({q["question_id"]: run.get("error")})
                continue
            stage = [s for s in run["stages"] if s["stage"] == "compile"]
            checks["max_message_chars"] = max(checks["max_message_chars"], stage[0]["message_chars"] if stage else 0)
            for e in run["prepared"]["evidence"]:
                checks["foreign_evidence"] += e["id"] not in sessions
                checks["wrong_dates"] += sessions.get(e["id"]) != e.get("date")
            checkpoint["run_ids"][q["question_id"]] = run["run_id"]
        checkpoint["completed_histories"][hid] = reactor_history
    server.shutdown()
    workflow.atomic_json(out / "checkpoint.json", checkpoint)
    report = {"split": str(split), "questions": len(questions), "histories": len(groups), "dry_run": args.dry_run,
              "harness_mode": cfg.mode(), "config_hash": cfg.hash(), "checks": checks}
    if not args.dry_run and checks["failed"] == 0:
        export = workflow.export_predictions(qfile, split / "histories.jsonl", out / "checkpoint.json",
                                             out / "predictions.jsonl", args.citation_fallback)
        fb = export.get("citation_fallback", {})
        report["export"] = {"valid": export["valid"], "predictions": export["predictions"],
                            "abstained": export["abstained"], "warnings": len(export["warnings"]),
                            "citation_fallback_enabled": fb.get("enabled"),
                            "fallback_filled": sum(bool(d["filled"]) for d in fb.get("decisions", [])),
                            "fallback_left_empty": sum(not d["filled"] for d in fb.get("decisions", []))}
        gold = split / "gold.jsonl"
        if gold.exists():
            ids = {q["question_id"] for q in questions}
            sub = out / "gold.subset.jsonl"
            sub.write_text("".join(l for l in gold.read_text(encoding="utf-8").splitlines(keepends=True)
                                   if json.loads(l)["question_id"] in ids), encoding="utf-8")
            proc = subprocess.run([sys.executable, str(ROOT / "score.py"), str(sub), str(out / "predictions.jsonl")],
                                  capture_output=True, text=True)
            report["score_py"] = proc.stdout.strip()
    (out / "offline_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "score_py"}, indent=1))
    if "score_py" in report:
        print("\nofficial score.py on the same subset (fake answers: plumbing only):\n" + report["score_py"])
    return 0 if checks["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
