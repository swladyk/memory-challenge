# Retrieval framework × team front (dashboard, harness API, submission workflow)

Branch `feat/retrieval-framework`: the framework (commit `79bd0d7`) merged with `team/front` (`a6ed918`) in
`a3de48c`; the wiring below is on top. Nothing is pushed.

## What each side brings

| Team `front` | This framework (`src/memory_challenge/`) |
| --- | --- |
| `dashboard/server.py`: local UI + `/api/v1` (holds the Reactor key and current history, one run at a time), records every run with its exact outgoing message | retrieval: windows, BM25, recovery, strength-gated packing to the exact message length, date notes |
| `dashboard/evaluation.py`: harness client (`halo.harness.v1`), message compiler (official `build_message`, 16,000 check), Reactor transport (official `ask_retrying`, 300 s) | `memret harness`: a `halo.harness.v1` server that answers the dashboard with our packed evidence |
| `submission/workflow.py`: per-history checkpointed runs through the API, strict export (provenance, dates, ownership), validation, zip packaging | `memret run-reactor` / `export-predictions`: sequential runner with an attempt log and per-question resume; kit converter + supported-citation fallback |
| `reactor/LOCAL_SETUP.md`: team hAlo **t05**, agent **agt_afcb0e0cb62f62db**, base URL `https://t05.api.reactor-hackathon.rxai.dev` (also in `dashboard/evaluation.py`, `reactor/smoke_test.py`) | `docs/live_runbook.md` |

## Wiring added

1. **Harness server** — `python -m memory_challenge harness --split test [--config configs/baseline.toml] --port 8890`
   (`src/memory_challenge/harness.py`). The contract carries `question_id`, `original_prompt`, `question_date` and
   `context.dataset_history_id`, which is all the retriever needs. It returns `prompt` = question + guidance + date
   notes, `question_date` unchanged, `evidence` = packed passages with this history's session ids and dates, and
   writes payloads/traces for `memret why`. Missing or unknown `dataset_history_id` → HTTP 422, so the dashboard fails
   closed. Incoming `evidence` is ignored: use the workflow's `--evidence-mode none`. Loopback only; `HARNESS_TOKEN`
   supported as in the team example.
2. **Workflow citation fallback** — `submission/workflow.py run|export|package --citation-fallback` (opt-in, off by
   default). When Reactor's final answer has no `[E#]` label, it cites only sessions among the evidence Reactor was sent
   whose text supports the answer (`reactor/citation_fallback.py`; organiser-sanctioned 2026-10-30), and records every
   decision in the export's validation report. `package` reproduces the export with the same flag.
3. **TLS** — our client subprocess sets `SSL_CERT_FILE` from certifi like the team's smoke test (verification on).
4. **Offline driver** — `tools/team_pipeline_offline.py` runs the team pipeline end to end with our harness and a fake
   Reactor transport (or `--dry-run`), then the workflow's strict export and the official `score.py`.

## Two ways to run live (one agent: never both at once)

| | A. Framework runner (`docs/live_runbook.md`) | B. Team pipeline (dashboard + our harness + workflow) |
| --- | --- | --- |
| Evidence | our retriever (payloads built first) | our retriever (via the harness, per question) |
| Key held by | the client subprocess, from the terminal's environment | the dashboard server process |
| On a timeout/closed chat | logged as `ambiguous`, run continues; re-ask later with `--retry-ambiguous` | the workflow stops; rerunning repeats that whole history (all its questions) |
| Resume | per question | per completed history |
| Export | kit `predictions.py` (+ fallback); missing questions reported, file still written | strict: every question must have a completed answer or nothing is exported |
| Extras | `memret why` traces | dashboard UI, recorded wire messages, zip `package` with code and runs |

Both send the same evidence for the same config. **Recommendation for today's deadline: A for the 146-question run**
(it degrades gracefully and resumes per question), and B's `package` only if the team wants its archive (it needs a
B run). Either way the submission is `predictions.jsonl` + code link + presentation (`README.md:85-99`).

## Verified offline (no Reactor)

| Check | Result |
| --- | --- |
| Framework tests (`python -m unittest discover -s tests -t tests`) | 42 pass (incl. 4 harness tests: contract, history binding, fail-closed errors, token, and the dashboard's own `prepare`/`compile_message` accepting our response) |
| Team tests: `unittest discover -s submission`, `dashboard/test_server.py`, `node dashboard/test_metrics.cjs` | 14 pass (10 theirs + 4 fallback), 7 pass, passed |
| Team pipeline, 20 dev questions, fake Reactor, `--citation-fallback` | 20/20 runs completed; 0 foreign sessions; 0 wrong dates; max message 15,895; strict export valid (20 predictions); fallback filled the 7 unlabelled answers; official `score.py` ran (fake answers: plumbing only) |
| Team pipeline, all 146 test questions, dry run | 146/146 prepared and compiled; max message 15,993 ≤ 16,000; 0 foreign sessions; 0 wrong dates; harness index p50 161 ms per history, retrieval p50 21 ms |
| Framework runner, 20 dev histories (22 questions), fake backend | 22 answered; export valid |
