# Retrieval framework: how to run it

Code: `src/memory_challenge/` · configs: `configs/` · tests: `tests/` · decisions: `docs/adr-001-retrieval-framework.md`
· measured results: `docs/framework_results.md` · team dashboard/workflow integration and `memret harness`:
`docs/integration.md` · live run: `docs/live_runbook.md`.

Status legend used below: **tested** (unit/integration tests), **measured** (numbers in `docs/framework_results.md`),
**implemented, not run** (code exists, needs a download or approval), **proposed** (not built).

## Setup

Python 3.11+ only; the baseline needs **no third-party package**.

```bash
export PYTHONPATH=src            # or: python -m venv .venv && .venv/bin/pip install -e '.[dev]'
python -m memory_challenge --help
python -m unittest discover -s tests -t tests     # or: python -m pytest -q
```

Data: download `dev`, `dev-large`, `test` from the repository's Releases page into the repo root (gitignored), as in
`README.md:13-15`.

Optional extras, installed into a project-local virtual environment, never globally:

| Extra | Installs | Needed for |
| --- | --- | --- |
| `.[reactor]` | `websockets>=14` | live Reactor calls through the kit's `reactor/client.py` |
| `.[dense]` | `numpy`, `sentence-transformers` (pulls in PyTorch) | `dense.enabled` and `rerank.enabled` (downloads models on first use) |
| `.[dev]` | `pytest` | running tests with pytest (unittest works without it) |

## Pipeline

```
question → plan (rule hints, anchors, components, ordinal, time range)
         → BM25 over windows (+ per-component searches) [+ dense] → reciprocal rank fusion
         → soft time boost → [rerank] → [bounded graph expansion] → [PPR for aggregation/temporal/update plans]
         → heuristic coverage check → at most one recovery round for uncovered components
         → strength-gated packing to the exact client message length → payload (+ trace, manifest)
         → Reactor (one history per dataset history, sequential per agent) → attempts log → results.jsonl
         → kit predictions.py (via a session-id adapter) → strict validation → (dev) official score.py
```

| Layer | Module | Status |
| --- | --- | --- |
| Ingestion, validation | `data/loader.py`, `data/validation.py` | tested |
| Windows (lists, ordinals, lead-ins, short-reply context, exact spans) | `data/windows.py` | tested, measured |
| Dates and conservative relative-date resolution | `data/dates.py`, `retrieval/temporal.py` | tested |
| BM25 | `indexes/lexical.py`, `indexes/history_index.py` | tested, measured |
| Dense retrieval (exact search, cached embeddings) | `indexes/dense.py` | implemented, not run (needs download) |
| RRF fusion with per-source weights, ranks and provenance | `retrieval/fusion.py` | tested, measured |
| Cross-encoder rerank | `retrieval/rerank.py` | implemented, not run (needs download) |
| Structural graph + bounded typed expansion | `graph/builder.py`, `graph/traversal.py` | tested, measured |
| Personalized PageRank (local, HippoRAG-inspired, not a reproduction) | `graph/pagerank.py` | measured (ablation only; no unit test) |
| Planner, controller, coverage, recovery | `retrieval/planner.py`, `controller.py`, `coverage.py` | tested, measured |
| Packing against the official message builder | `retrieval/packing.py`, `reactor/message.py` | tested, measured |
| Reactor backends, scheduler, checkpoints | `reactor/backends.py`, `scheduler.py`, `checkpoints.py` | tested with the fake backend; live path not run |
| Prediction export and validation | `reactor/predictions.py` | tested (kit converter + official scorer) |
| Supported-citation fallback (organiser-sanctioned) | `reactor/citation_fallback.py` | tested; not measured on real Reactor answers |
| Evaluation and ablations | `evaluation/`, `cli.py` | measured |
| Claim/event extraction, UPDATES/CONTRADICTS edges, hierarchies, compression | — | proposed |

## Commands

All commands accept `--config configs/<name>.toml` (defaults when omitted). Outputs go under `runs/` (gitignored).

| Command | What it does |
| --- | --- |
| `inspect-data --split dev` | validates fields, ids, dates, ordering and question links; prints counts |
| `build-index --split dev-large [--limit N]` | builds windows + BM25 (and graph/embeddings if enabled) per history; reports build times |
| `retrieve --split test --out runs/test-baseline` | writes `payloads/<qid>.json` (evidence in the participant format, question text, manifest), `traces/<qid>.json`, `manifest.json` |
| `why --run runs/test-baseline --question-id q_...` | plan, coverage, packed passages with provenance, top candidates with per-source ranks and strengths, exclusion reasons |
| `evaluate-retrieval --split dev-large --partition tune` | session recall@k / all@k, packed recall/precision/F1/all-gold, answer-text-in-pack, chars, latency; per type |
| `run-ablation --split dev-large --partition tune --configs configs/*.toml` | the same metrics for several configs on the same questions; a config that can't run is listed with its error |
| `run-reactor --payloads runs/... [--backend fake]` | sends payloads; `fake` (default) contacts nothing; `live` needs `--allow-live` and uses the team's one agent |
| `export-predictions --run runs/.../reactor-live --payloads runs/... --histories test/histories.jsonl --questions test/questions.jsonl --out predictions.jsonl` | kit converter with `--abstain-on agent`, supported-citation fallback (with `--payloads`), then strict validation |
| `validate-predictions --predictions ... --questions ... --histories ... [--gold dev/gold.jsonl]` | schema, ids, duplicates, citations ⊂ the question's own history; with gold, runs the official `score.py` |

`--partition tune|lockbox|all` splits by history id (dev and dev-large share history ids; cued/uncued twins share a
history), so paired questions stay together. Use `tune` for every decision and `lockbox` rarely.

## Live logs and the Live evaluation view

`evaluate-retrieval`, `run-ablation` (one folder per config), `retrieve` and `run-reactor` write three files into
their run folder **while they run**, appended, flushed and fsynced per question (`src/memory_challenge/live.py`):

| File | One line per | Holds |
| --- | --- | --- |
| `run.json` | run (rewritten atomically) | kind, command, split, config and hash, total, `has_gold`, status `running`/`finished`/`failed` |
| `events.jsonl` | question event | retrieval: question id, type, hints, passages, chars, latencies, per-question metrics. Reactor: `processed` (sent), `attempt` (will be retried), `evaluated`/`graded` (final), with outcome, latency, retries, citation count, whether the citation fallback would fill it and, with gold, the local grade |
| `stats_history.jsonl` | event | `progress` {total, processed, evaluated, graded (null without gold), elapsed, ETA, rate}, cumulative metrics per question type, latency percentiles; Reactor runs add outcome counts |

They hold ids, labels and numbers only: no conversation text, answers or keys. The stats history is meant for later
analysis (for example `pandas.read_json(path, lines=True)`). For Reactor runs give `run-reactor --gold dev/gold.jsonl`
to grade each final answer with `score.py`'s own functions as it arrives; those scores are local and **unofficial**
(answers `score.py` can't match exactly are `unjudged`). A resumed run appends to the same files.

To watch a run, start the team dashboard and open **Live evaluation** (`http://127.0.0.1:8873/#live`, or the
port in `HALO_DASHBOARD_PORT`):

```bash
.venv/bin/python dashboard/server.py
```

The view follows the newest run under `runs/` (`MEMRET_RUNS_DIR` to change it) and refreshes once a second: staged
progress bars (processed / evaluated / graded, each N / total and %, with ETA), the per-type metrics table, charts
of the stats history and a feed of the latest questions. It reads the files through read-only endpoints behind the
dashboard's usual auth: `GET /api/v1/live/runs`, `/api/v1/live/runs/{id}`, `/api/v1/live/runs/{id}/events?after=N`
and `/stats?after=N` (`?tail=N` for the last lines). Nested runs have ids like `ablation-dev-large~baseline`.

## Comparing with the kit baseline

`compare` puts systems side by side on the same questions with score.py's own `validate`, `score_question` and
`summarize`: per-type scores with deltas against a reference, a per-question answer transition table (correct /
unjudged / wrong / abstained / missing), evidence-F1 wins and losses, and paired bootstrap 95% intervals. Given
payload directories it also compares what each system sent (gold sessions in the packed evidence, passages, message
size), which needs no Reactor run. The gold file must hold exactly the compared questions: score.py counts a missing
prediction as wrong.

```bash
.venv/bin/python -m memory_challenge compare --gold G --reference kit \
    --system kit=kit/predictions.jsonl --system ours=ours/predictions.jsonl \
    --payloads kit=runs/kit --payloads ours=runs/ours --out runs/compare
```

Ready-made for a scorable live comparison: 40 dev-large lockbox questions (never used for tuning; 4 unanswerable),
evidence built by both systems in `runs/dev-live` (ours, `configs/baseline.toml`) and `runs/dev-live-kit` (the kit's
`run_benchmark.py` retriever, `configs/a-kit.toml`), each with `gold.subset.jsonl`:

```bash
bash scripts/start_dev_eval_run.sh kit     # live, ~60-80 min each; needs agent t05 free
bash scripts/start_dev_eval_run.sh         # ours
bash scripts/score_dev_eval_run.sh kit && bash scripts/score_dev_eval_run.sh   # export, validate, score.py
bash scripts/compare_dev_runs.sh           # report in runs/dev-compare/reactor-live/comparison.md
```

`correct` from score.py is a lower bound (the official LLM judge decides `unjudged` answers); evidence F1 and
abstention are exact. Retrieval only, all 163 lockbox questions (`run-ablation --partition lockbox`): complete gold
evidence packed 0.401 (kit) vs 0.836 (ours), gold recall 0.533 vs 0.898, precision 0.290 vs 0.363.

## Configurations

| Config | Mode | Use · measured on dev-large tune (`docs/framework_results.md`) |
| --- | --- | --- |
| `baseline.toml` | lexical+recovery | the dependency-free default · complete evidence packed 0.834, precision 0.407 |
| `a-kit.toml` | kit-keyword | ablation A: the supplied example retriever · 0.465 |
| `b-lexical-windows.toml` | lexical | ablation B: windows + BM25 + packing only · 0.831 |
| `b2-attribute-expansion.toml` | lexical+recovery | baseline + attribute query expansion · 0.843 (within noise; re-check on lockbox) |
| `g-no-time-boost.toml` | lexical+recovery | baseline without the soft time-range boost · identical to baseline |
| `f-graph.toml` | lexical+graph+recovery | ablation F: bounded structural expansion · no change in packed sessions; +0.6 s cold latency |
| `h-ppr.toml` | lexical+graph+ppr+recovery | ablation H: PPR as an extra fusion source · no change; +3.0 s cold latency |
| `p-floor-030.toml` | lexical+recovery | lower strength floor · 0.840, precision 0.296 |
| `p-budget-32k.toml` | lexical+recovery | server-size messages · 0.861 (temporal +0.067), ~20k chars; needs the limit confirmed |
| `hybrid.toml` | lexical+dense+rerank+graph+recovery | needs `.[dense]` and model downloads · not run |

Every run records `config_hash` (all retrieval-relevant settings) and `retrieval_mode` (what actually ran).

## Dry run (no network)

```bash
export PYTHONPATH=src
python -m memory_challenge retrieve --split dev --limit 20 --out runs/dryrun-dev
python -m memory_challenge run-reactor --payloads runs/dryrun-dev --backend fake
python -m memory_challenge export-predictions --run runs/dryrun-dev/reactor-fake \
    --histories dev/histories.jsonl --questions dev/questions.jsonl --out runs/dryrun-dev/predictions.jsonl
python -m memory_challenge validate-predictions --predictions runs/dryrun-dev/predictions.jsonl \
    --questions dev/questions.jsonl --histories dev/histories.jsonl --gold dev/gold.jsonl
```

The fake backend quotes a line of its best-matching passage and cites it; its "answers" exercise the protocol and
the converter, and say nothing about answer quality.

## Live run (not executed; needs approval, the team URL/agent id and the key)

Step-by-step, with a dev smoke test first, timings and resume: **`docs/live_runbook.md`**. In short:

```bash
python -m venv .venv && .venv/bin/pip install -e '.[reactor]'
printf 'Access key: '; read -rs REACTOR_ACCESS_KEY; echo; export REACTOR_ACCESS_KEY   # hidden prompt; never on a command line
export PYTHONPATH=src REACTOR_BASE_URL=https://tNN.api.reactor-hackathon.rxai.dev REACTOR_AGENT_IDS=agt_...
.venv/bin/python -m memory_challenge retrieve --split test --config configs/baseline.toml --out runs/test-live
caffeinate -i .venv/bin/python -m memory_challenge run-reactor --payloads runs/test-live --config configs/baseline.toml \
    --backend live --allow-live
.venv/bin/python -m memory_challenge export-predictions --run runs/test-live/reactor-live --payloads runs/test-live \
    --histories test/histories.jsonl --questions test/questions.jsonl --out submission/predictions.jsonl
```

What the organisers said (event channel, 2026-10-30) and how the harness follows it:

| Organisers | Harness |
| --- | --- |
| Each team has **one** agent; agents process questions sequentially | one agent id; strictly one question at a time; one Reactor history per dataset history, never reset between its questions |
| An answer takes ~50–60 s, plus 15–30 s to finish the agent's internal cycle; the 146-question benchmark takes ~2.5 h or more | plan ~3–6 h for test (latency grows within a history, below) |
| (2026-10-31) Latency grows as a history accumulates questions (~60 → ~200 s, occasionally > 300 s); "A 300s timeout is the client's waiting limit"; increase `--timeout`, e.g. to 600 s; a client timeout doesn't cancel the question, so check late replies before retrying, and retry in the same Reactor history once the previous request has cleared | `reactor.timeout_s` = 600; late finals are salvaged (`answered_late`); a still-unanswered timeout gets a drain ping, then `reactor.reask_timed_out` = 1 re-ask in the same Reactor history (`reactor/scheduler.py` docstring) |
| `previous_pending` right after an answer is expected; retry with a 5–10 s backoff | the client retries it within its timeout; if it still comes back, the scheduler waits `refused_backoff_s` = 7 s and retries up to `retry_refused` = 3 more times |
| "If those labels are missing, your code can select supporting sessions from the retrieved evidence: just make sure they actually support Reactor's answer rather than blindly falling back to the top-ranked result." | `export-predictions --payloads`: supported-citation fallback (below) |

- Re-running the same command resumes: dataset histories whose questions all have a final answer are skipped; each
  one with questions left gets a new Reactor history and is asked from its first question. A Reactor history id is
  never retried once stale: not on resume, and not after `history_changed` mid-run, which also restarts the dataset
  history from its first question in a new history (organisers, 2026-10-31). After `reactor.max_history_restarts`
  (2) restarts of one dataset history the run stops, since another process keeps resetting the agent. Run one
  benchmark process at a time. Attempts from another configuration are never combined. Live runs log every request
  in `requests.jsonl`; `tools/reactor_connection_monitor.py` logs every connection from the machine.
- Outcomes are kept apart: `answered`, `reactor_abstained` (INSUFFICIENT_EVIDENCE), `answered_late` (a timed-out
  question's final answer, reported with a later request in the same history; citations mapped against its own
  passages with the client's `citations()`), `ambiguous` (timeout/closed, no answer even after the ping and the
  re-ask), `not_delivered` (refused before reaching the agent; retried as above), `error`. A direct answer outranks a
  salvaged one, which outranks a timeout.
- Export uses `--abstain-on agent` only: timeouts and errors are reported as missing, not converted into abstentions.
  Pass `--abstain-on agent,no-answer` deliberately if the team decides otherwise.
- For `p-budget-32k.toml`, the client is called with `--max-chars` from the payload; only use it once the
  organisers confirm the server limit (`reactor/PARTICIPANTS.md:119-120`).

### Supported-citation fallback

The client maps only `[E#]` labels written in the final answer (`reactor/client.py:261-268`). When Reactor answers
without usable labels (none, labels never sent, or passages without ids), the prediction has no citations and scores
evidence F1 = 0. With `--payloads`, `export-predictions` then looks at **that question's own packed passages** and
cites the sessions whose quoted text supports Reactor's answer (`reactor/citation_fallback.py`):

- answer units = numbers (`25:50`, `2.5`, `1,200`) and stemmed content words of Reactor's answer;
- a passage supports the answer if it contains at least `--min-support` (default **0.6**) of those units, at least one
  of them *distinctive* (a number with two or more digits or a separator, a capitalised word, or a word of five or more
  letters), in its quoted text; computed `[note: ...]` lines don't count;
- every supporting passage is cited, best first, at most `--max-fallback-sessions` (default 3); sessions outside the
  question's history are never cited;
- if nothing qualifies, citations stay empty: **no fallback to the top-ranked passage**. Answers, abstentions and
  existing citations are never changed.

The kit's own output is kept as `<out>.kit.jsonl`, and every decision (why the answer had no citation, support
share, matched units, rejected passages) is written to `<out>.citation_fallback.json`. Status: tested with the fake
backend and the kit converter; not yet measured against real Reactor answers.

## Optional models (not downloaded)

`hybrid.toml` uses `BAAI/bge-small-en-v1.5` (bi-encoder, ~130 MB) and `cross-encoder/ms-marco-MiniLM-L-6-v2`
(~90 MB). After approval:

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dense]'
PYTHONPATH=src .venv/bin/python -m memory_challenge run-ablation --split dev-large --partition tune \
    --configs configs/baseline.toml configs/hybrid.toml
```

Without the packages, `hybrid.toml` fails immediately with an install instruction (`OptionalDependencyError`); the
ablation table lists it as not run. Embeddings are cached under `.cache/embeddings/<model+revision+window version>/`
by window-text hash. Exact search is used: ≤ ~10k windows per history makes an ANN index unnecessary.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `reactor/predictions.py` exits 2 with "Unterminated string" on a histories file | the kit splits `--histories` with `str.splitlines()`, which breaks on U+2028 inside conversation text (present in dev, dev-large and test). `export-predictions` passes it a session-id-only file instead; use that command rather than calling the kit directly on `histories.jsonl` |
| `refusing to call Reactor` | add `--allow-live` (and set the key, URL and agent ids) |
| `OptionalDependencyError` | install the extra in `.venv` or disable the feature |
| `payloads come from N different configurations` | run each configuration's payloads in its own run directory |
| `message_builder: mirror` in a manifest | `reactor/client.py` could not be imported; lengths use the mirrored template, checked by `tests/test_message.py` |

## Reproducing reported numbers

```bash
export PYTHONPATH=src
python -m memory_challenge run-ablation --split dev-large --partition tune --workers 8 \
    --out runs/ablation-dev-large-tune --configs configs/a-kit.toml configs/b-lexical-windows.toml \
    configs/baseline.toml configs/b2-attribute-expansion.toml configs/g-no-time-boost.toml configs/f-graph.toml \
    configs/h-ppr.toml configs/p-floor-030.toml configs/p-budget-32k.toml configs/hybrid.toml
```

Results are deterministic for a given commit, config and data (no randomness in retrieval).
