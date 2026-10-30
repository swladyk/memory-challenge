"""Command line: ``python -m memory_challenge <command> ...`` (or ``memret`` after ``pip install -e .``).

Commands: inspect-data, build-index, retrieve, why, evaluate-retrieval, run-reactor, export-predictions,
validate-predictions, run-ablation. Outputs go under ``runs/`` (gitignored: payloads and traces contain conversation
text). Live Reactor calls need ``--backend live --allow-live`` and REACTOR_ACCESS_KEY in the environment.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

from .config import Config, load_config

REPO = Path(__file__).resolve().parents[2]


def _cfg(args) -> Config:
    return load_config(args.config)


def _out(path: str | None, default: str) -> Path:
    return Path(path) if path else REPO / "runs" / f"{default}-{time.strftime('%Y%m%d-%H%M%S')}"


def cmd_inspect(args) -> int:
    from .data.validation import validate_split
    report = validate_split(Path(args.split))
    print(json.dumps(report, indent=1))
    return 0 if report["error_count"] == 0 else 1


def cmd_build_index(args) -> int:
    from .data.loader import SplitPaths, iter_histories
    from .indexes.history_index import HistoryIndex
    cfg = _cfg(args)
    paths = SplitPaths(Path(args.split))
    times, windows, graphs = [], [], []
    for n, h in enumerate(iter_histories(paths.histories)):
        if args.limit and n >= args.limit:
            break
        idx = HistoryIndex(h, cfg)
        times.append(idx.build_seconds)
        windows.append(len(idx.windows))
        if cfg.graph.enabled or cfg.ppr.enabled:
            t0 = time.perf_counter()
            g = idx.graph
            graphs.append((time.perf_counter() - t0, sum(len(v) for v in g.adj.values())))
        if cfg.dense.enabled:
            from .indexes.dense import DenseIndex
            DenseIndex(cfg).embed_docs([w.index_text for w in idx.windows])
    report = {"histories": len(times), "mode": cfg.mode(), "windows_mean": round(statistics.fmean(windows), 1),
              "build_ms_p50": round(1000 * statistics.median(times), 1), "build_ms_max": round(1000 * max(times), 1),
              "note": "lexical indexes are rebuilt per history at query time (cheaper than loading); dense "
                      "embeddings, when enabled, are cached under dense.cache_dir"}
    if graphs:
        report["graph_build_ms_p50"] = round(1000 * statistics.median(g[0] for g in graphs), 1)
        report["graph_edges_mean"] = round(statistics.fmean(g[1] for g in graphs), 1)
    print(json.dumps(report, indent=1))
    return 0


def cmd_retrieve(args) -> int:
    from .evaluation.runner import run_split
    from .manifest import build_manifest, write_json
    cfg = _cfg(args)
    out = _out(args.out, f"retrieve-{cfg.name}")
    qids = set(args.question_id) if args.question_id else None
    recs = run_split(Path(args.split), cfg, args.workers, args.partition, args.limit, out, qids)
    split = Path(args.split)
    write_json(out / "manifest.json", build_manifest(cfg, {"histories": split / "histories.jsonl",
                                                           "questions": split / "questions.jsonl"},
                                                     {"command": "retrieve", "questions": len(recs)}))
    print(f"{len(recs)} payload(s) and trace(s) written to {out}")
    if len(recs) == 1:
        return cmd_why(argparse.Namespace(run=str(out), question_id=recs[0]["question_id"]))
    return 0


def cmd_why(args) -> int:
    run = Path(args.run)
    tr = json.loads((run / "traces" / f"{args.question_id}.json").read_text(encoding="utf-8"))
    pl = json.loads((run / "payloads" / f"{args.question_id}.json").read_text(encoding="utf-8"))
    print(f"question {args.question_id} · mode {tr['mode']} · message {pl['message_chars']}/{pl['max_chars']} chars "
          f"({pl['message_builder']} builder)")
    print("plan:", json.dumps(tr["plan"]))
    for c in tr["coverage"]:
        print(f"coverage [{c['method']}]: {c['component']!r} covered={c['covered']}")
    for n in tr["notes"]:
        print("note:", n)
    print("\npacked:")
    for m in pl["manifest"]:
        print(f"  {m['label']} {m['session_id']} {m['date']} windows={m['windows']} via={m.get('via')}")
    print("\ntop candidates (window · session · fused score · strength · ranks · via):")
    for c in tr["candidates"][:15]:
        print(f"  {c['window']} · {c['session']} · {c['score']} · {c['strength']} · {c['ranks']} · {c['via']}")
    print("\nnot packed (first 10):")
    for e in tr["excluded"][:10]:
        print(f"  {e['window']}: {e['reason']}")
    return 0


def _evaluate(split: Path, cfg: Config, args, out: Path | None) -> tuple[dict, list[dict]]:
    from .data.loader import load_gold
    from .evaluation.metrics import score_record, summarize
    from .evaluation.runner import run_split
    t0 = time.perf_counter()
    recs = run_split(split, cfg, args.workers, args.partition, args.limit, out)
    wall = time.perf_counter() - t0
    gold = load_gold(split / "gold.jsonl")
    scored = [score_record(r, gold[r["question_id"]]) for r in recs]
    summary = summarize(scored)
    summary["_run"] = {"config": cfg.name, "config_hash": cfg.hash(), "mode": cfg.mode(), "split": str(split),
                       "partition": args.partition, "limit": args.limit, "questions": len(recs),
                       "wall_s": round(wall, 1), "workers": args.workers}
    return summary, scored


def cmd_evaluate(args) -> int:
    from .evaluation.metrics import markdown_table
    from .manifest import build_manifest, write_json
    cfg = _cfg(args)
    split = Path(args.split)
    if not (split / "gold.jsonl").exists():
        print("evaluate-retrieval needs a split with gold.jsonl (dev or dev-large)", file=sys.stderr)
        return 2
    out = _out(args.out, f"eval-{cfg.name}-{split.name}")
    summary, scored = _evaluate(split, cfg, args, out if args.save_payloads else None)
    write_json(out / "summary.json", summary)
    (out / "per_question.jsonl").write_text("".join(json.dumps(s) + "\n" for s in scored), encoding="utf-8")
    md = markdown_table(summary, f"{cfg.name} · {cfg.mode()} · {split.name} · {args.partition}")
    (out / "summary.md").write_text(md + "\n", encoding="utf-8")
    write_json(out / "manifest.json", build_manifest(cfg, {"histories": split / "histories.jsonl",
                                                           "questions": split / "questions.jsonl",
                                                           "gold": split / "gold.jsonl"}, {"command": "evaluate-retrieval"}))
    print(md)
    print(f"\nwritten to {out}")
    return 0


def cmd_ablation(args) -> int:
    from .manifest import write_json
    split = Path(args.split)
    out = _out(args.out, f"ablation-{split.name}")
    rows = []
    for path in args.configs:
        try:
            cfg = load_config(path)
            summary, _ = _evaluate(split, cfg, args, None)
            rows.append({"config": cfg.name, "mode": cfg.mode(), "hash": cfg.hash(), "summary": summary})
            print(f"{cfg.name}: done ({summary['_run']['wall_s']} s)", flush=True)
        except Exception as exc:  # e.g. an optional model that isn't installed: recorded, not hidden
            rows.append({"config": Path(path).stem, "error": f"{type(exc).__name__}: {exc}"})
            print(f"{Path(path).stem}: not run: {exc}", flush=True)
    write_json(out / "ablation.json", rows)
    cols = [("all", "pack_all"), ("all", "pack_recall"), ("all", "pack_precision"), ("all", "answer_text_in_pack"),
            ("multi-session", "pack_all"), ("temporal-reasoning", "pack_all"), ("knowledge-update", "pack_all"),
            ("single-session-assistant", "answer_text_in_pack"), ("all", "all@10"), ("all", "message_chars_mean")]
    head = "| config | mode | " + " | ".join(f"{t.split('-')[0] if t != 'all' else 'all'} {k}" for t, k in cols) + \
           " | retrieval p50 ms | p95 ms |"
    lines = [head, "|" + "---|" * (len(cols) + 4)]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['config']} | not run: {r['error'][:80]} |" + " |" * (len(cols) + 2))
            continue
        s = r["summary"]
        lat = s["_latency"]
        lines.append(f"| {r['config']} | {r['mode']} | " + " | ".join(
            "—" if s.get(t, {}).get(k) is None else str(s[t][k]) for t, k in cols) +
            f" | {lat['retrieval_ms_p50']} | {lat['retrieval_ms_p95']} |")
    md = "\n".join(lines)
    (out / "ablation.md").write_text(md + "\n", encoding="utf-8")
    print(md)
    print(f"\nwritten to {out}")
    return 0


def cmd_run_reactor(args) -> int:
    from .reactor.backends import ClientBackend, FakeBackend
    from .reactor.scheduler import load_payloads, run
    cfg = _cfg(args)
    payload_dir = Path(args.payloads) / "payloads" if (Path(args.payloads) / "payloads").exists() else Path(args.payloads)
    payloads = load_payloads(payload_dir)
    if not payloads:
        print(f"no payloads in {payload_dir}", file=sys.stderr)
        return 2
    run_dir = Path(args.run_dir) if args.run_dir else Path(args.payloads) / f"reactor-{args.backend}"
    if args.backend == "live":
        if not args.allow_live:
            print("refusing to call Reactor: add --allow-live to confirm a real run", file=sys.stderr)
            return 2
        if not os.environ.get("REACTOR_ACCESS_KEY"):
            print("set REACTOR_ACCESS_KEY at a hidden prompt first (reactor/PARTICIPANTS.md, step 2)", file=sys.stderr)
            return 2
        base = args.base_url or os.environ.get(cfg.reactor.base_url_env)
        agents = args.agent_id or [a for a in os.environ.get(cfg.reactor.agent_ids_env, "").split(",") if a]
        if not base or not agents:
            print("give --base-url and --agent-id (or REACTOR_BASE_URL / REACTOR_AGENT_IDS)", file=sys.stderr)
            return 2
        backends = [ClientBackend(REPO / cfg.reactor.client_path, base, a, args.timeout or cfg.reactor.timeout_s)
                    for a in agents]
    else:
        backends = [FakeBackend(agent_id=f"agt_fake{i}") for i in range(args.fake_agents)]
    summary = run(payloads, backends, run_dir, cfg.reactor.retry_refused,
                  args.retry_ambiguous or cfg.reactor.retry_ambiguous, cfg.reactor.refused_backoff_s)
    print(json.dumps({**summary, "run_dir": str(run_dir), "backend": args.backend}, indent=1))
    return 0


def cmd_export(args) -> int:
    from .reactor.predictions import export, validate
    run_dir = Path(args.run)
    results = run_dir / "results.jsonl" if run_dir.is_dir() else run_dir
    out = Path(args.out)
    abstain = tuple(a for a in args.abstain_on.split(",") if a) if args.abstain_on else ()
    payloads = None
    if args.payloads:
        payloads = Path(args.payloads) / "payloads" if (Path(args.payloads) / "payloads").exists() else Path(args.payloads)
    rep = export(results, Path(args.histories), out, abstain, payloads=payloads, min_support=args.min_support,
                 max_sessions=args.max_fallback_sessions)
    print(json.dumps(rep, indent=1))
    if rep["exit_code"] == 2 or not out.exists():
        print("the kit's predictions.py wrote nothing (see summary above)", file=sys.stderr)
        return 2
    if args.questions:
        v = validate(out, Path(args.questions), Path(args.histories))
        print(json.dumps(v, indent=1))
        if not v["valid"]:
            return 1
    return 0 if rep["exit_code"] in (0, 1) else rep["exit_code"]


def cmd_validate(args) -> int:
    from .reactor.predictions import official_score, validate
    v = validate(Path(args.predictions), Path(args.questions), Path(args.histories))
    print(json.dumps(v, indent=1))
    if args.gold and v["valid"]:
        print("\nofficial score.py (answers it can't match are 'unjudged'; the official judge is an LLM):")
        print(official_score(Path(args.gold), Path(args.predictions)))
    return 0 if v["valid"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memret", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, split=True):
        if split:
            p.add_argument("--split", required=True, help="split directory (dev, dev-large, test)")
        p.add_argument("--config", help="TOML config (configs/*.toml); defaults when omitted")
        p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
        p.add_argument("--partition", default="all", choices=["all", "tune", "lockbox"])
        p.add_argument("--limit", type=int, help="evenly spaced subset of this many histories")
        p.add_argument("--out", help="output directory (default: runs/<command>-<timestamp>)")

    p = sub.add_parser("inspect-data", help="validate a split and print counts")
    p.add_argument("--split", required=True)
    p.set_defaults(fn=cmd_inspect)
    p = sub.add_parser("build-index", help="build windows/BM25 (and graph, embeddings) per history; report timings")
    common(p)
    p.set_defaults(fn=cmd_build_index)
    p = sub.add_parser("retrieve", help="retrieve and pack evidence; write payloads, traces and a manifest")
    common(p)
    p.add_argument("--question-id", action="append")
    p.set_defaults(fn=cmd_retrieve)
    p = sub.add_parser("why", help="explain one question's retrieval from a retrieve run")
    p.add_argument("--run", required=True)
    p.add_argument("--question-id", required=True)
    p.set_defaults(fn=cmd_why)
    p = sub.add_parser("evaluate-retrieval", help="retrieval metrics against dev gold")
    common(p)
    p.add_argument("--save-payloads", action="store_true")
    p.set_defaults(fn=cmd_evaluate)
    p = sub.add_parser("run-ablation", help="evaluate several configs on the same questions")
    common(p)
    p.add_argument("--configs", nargs="+", required=True)
    p.set_defaults(fn=cmd_ablation)
    p = sub.add_parser("run-reactor", help="send payloads to Reactor (fake by default)")
    p.add_argument("--payloads", required=True, help="a retrieve run directory (or its payloads/ folder)")
    p.add_argument("--config")
    p.add_argument("--backend", choices=["fake", "live"], default="fake")
    p.add_argument("--allow-live", action="store_true", help="required for --backend live")
    p.add_argument("--base-url")
    p.add_argument("--agent-id", action="append", help="the team's agent id (each team has one agent)")
    p.add_argument("--timeout", type=float)
    p.add_argument("--fake-agents", type=int, default=1)
    p.add_argument("--retry-ambiguous", action="store_true")
    p.add_argument("--run-dir")
    p.set_defaults(fn=cmd_run_reactor)
    p = sub.add_parser("export-predictions", help="results.jsonl → predictions.jsonl via the kit's predictions.py")
    p.add_argument("--run", required=True, help="reactor run directory or results.jsonl")
    p.add_argument("--histories", required=True)
    p.add_argument("--questions", help="also validate against this questions.jsonl")
    p.add_argument("--out", required=True)
    p.add_argument("--abstain-on", default="agent", help="agent and/or no-answer; '' for none (default: agent)")
    p.add_argument("--payloads", help="the retrieve run (or its payloads/): enables the supported-citation fallback")
    p.add_argument("--min-support", type=float, default=0.6,
                   help="share of the answer's numbers/content words a passage must contain to be cited (default 0.6)")
    p.add_argument("--max-fallback-sessions", type=int, default=3)
    p.set_defaults(fn=cmd_export)
    p = sub.add_parser("validate-predictions", help="strict validation (+ official score.py with --gold)")
    p.add_argument("--predictions", required=True)
    p.add_argument("--questions", required=True)
    p.add_argument("--histories", required=True)
    p.add_argument("--gold")
    p.set_defaults(fn=cmd_validate)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
