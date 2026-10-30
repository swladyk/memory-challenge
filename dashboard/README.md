# hAlo Reactor Observatory

A local dashboard for chat transactions and benchmark evaluation. No frontend build or framework installation is required.

## Run

From the workspace root:

```sh
.venv/bin/python -m pip install -r reactor/requirements.txt
.venv/bin/python dashboard/server.py
```

Open http://127.0.0.1:8873. The server only listens on loopback.

- Existing smoke-test results and UI requests load from `results/`.
- Connection settings accept a private key for this page session, or start the server with `REACTOR_ACCESS_KEY` already in its environment. The key is never stored in results or browser storage.
- The saved Reactor history is reused. Creating a history resets agent memory and requires explicit confirmation in the UI.
- Edit evidence JSON and question date before sending. Requests run sequentially; the browser polls for completion. New results include the evidence sent.
- Import raw Reactor `results.jsonl` and optional `gold.jsonl` through Import data. Imports stay in the tab until refresh; export results to retain imported transactions. Duplicate IDs are rejected.

## Metrics

Operational metrics: completed/failed requests, mean reported agent latency, citation coverage among non-abstained answers, abstention rate among completed answers, and retries.

Reference metrics: normalized exact match including aliases, mean session-level evidence F1 on answerable gold questions, and correct abstention decisions over all gold questions. Missing predictions count as incorrect. Exact match is not the official LLM judge. No correctness score is invented when gold is absent.

The existing smoke test has two answers, mean latency 58.255 seconds, citation coverage 0%, and abstention rate 50%. The first response referenced evidence only in its explanation, so the client extracted no final-answer citations.

## Checks

```sh
node dashboard/test_metrics.cjs
.venv/bin/python dashboard/test_server.py
```

The frontend is in `index.html`. The Python standard-library HTTP server delegates model calls to the supplied `reactor/client.py`, with TLS verification using certifi. The dashboard's credentials are configured for hAlo's t05 host and agent.

## Refined interface

- Compact panels prioritize the current question, answer, and a pinned composer. Browse transactions opens an in-panel picker on small screens.
- Conversation, Evidence, Diagnostics, and JSON tabs separate response review from debugging. Use the arrow buttons to move between transactions.
- The evidence editor supports labeled excerpt fields and JSON mode. Cancel discards draft changes; Apply validates the excerpt structure.
- Copy answers or JSON, reuse saved evidence, filter missing citations, and send with Command/Ctrl + Enter.
- Connection state says “Key set” until a request verifies it. Pending requests keep the composer disabled while preserving access to existing results.
- Gold imports enable run-level metrics and an inline reference comparison on matching conversations. A text mismatch is not labeled a semantic failure.
- Frontend code is separated into `index.html`, `styles.css`, and `app.js`.

## Harness and evaluation API

See [API.md](API.md) for the complete request/response contract and runnable example harness.
The layers icon in the top bar configures the harness and previews the current prompt.
All new chat requests now pass through the evaluation pipeline. No harness is enabled by default.
Original prompts, prepared inputs, exact outgoing messages, results, stage timings, and reference scores are recorded in `results/runs/`.

New endpoints: `/api/v1/harness`, `/api/v1/runs`, `/api/v1/runs/{id}`, `/api/v1/runs/{id}/evaluate`, and `/api/v1/evaluations`.
External clients authenticate using the server's `HALO_API_TOKEN`; Reactor credentials remain separate.
