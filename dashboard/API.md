# Harness and evaluation API v1

The pipeline is:

`original user prompt → HTTP harness → prepared prompt/evidence → official Reactor message template → Reactor → deterministic evaluation`

The harness transforms inputs; Reactor remains the final answer generator. The exact wire message is recorded as `outbound_message: {"type":"message","text":"…"}`. The official client still handles WebSocket authentication, retries, reference markers, asynchronous responses, and citation parsing.

## Start and connect

The existing dashboard listens on **http://127.0.0.1:8873**. Use the layers icon in its top bar to configure a harness and preview the current composer prompt without calling Reactor. With no harness enabled, input passes through unchanged. Browser controls already use a session token.

For an external harness runner or benchmark client, start the server with a separate local API token and a Reactor credential. In zsh:

```sh
read -rs 'HALO_API_TOKEN?Choose a local API token: '
export HALO_API_TOKEN
read -rs 'REACTOR_ACCESS_KEY?Reactor access key: '
export REACTOR_ACCESS_KEY
.venv/bin/python dashboard/server.py
```

Stop the existing dashboard process before starting a replacement on the same port. Access keys and tokens are kept in process memory, not saved in run records. `HALO_API_TOKEN` authenticates your client to this API; it is not your Reactor key. The API remains loopback-only.

Start the working example harness in another terminal:

```sh
.venv/bin/python dashboard/examples/harness.py
```

Register it (or set `HARNESS_URL` before starting the dashboard):

```sh
curl -sS http://127.0.0.1:8873/api/v1/harness \
  -H "Authorization: Bearer $HALO_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"url":"http://127.0.0.1:8890/prepare","timeout_s":30}'
```

For an authenticated harness set `HARNESS_TOKEN` in both server environments, or supply `token` in the configuration body/UI. Tokens are not returned by GET. Omitting `token` preserves it; setting `token` to `""` clears it. Configuration is server-session scoped; environment variables configure startup defaults. Configure only trusted endpoints: prompts, context, and evidence are sent to them. HTTP is allowed only on loopback; remote endpoints must use HTTPS. Redirects are rejected.

## Endpoints

All `/api/v1/` requests require `Authorization: Bearer <HALO_API_TOKEN>` or the dashboard's `X-Reactor-Token` session header.

| Method | Path | Behavior |
|---|---|---|
| GET / POST | `/api/v1/harness` | Inspect / configure the harness callback |
| POST | `/api/v1/runs` | Submit original input; returns 202 and a poll URL |
| GET | `/api/v1/runs/{run_id}` | Full input, prepared output, wire message, result, stages, metrics |
| GET | `/api/v1/runs` | List persisted runs, oldest first |
| POST | `/api/v1/runs/{run_id}/evaluate` | Attach/replace reference and rescore; no model call |
| GET | `/api/v1/evaluations` | Aggregate per-run scores with denominators |
| POST | `/api/v1/history` | Explicitly reset Reactor history; returns 202 |
| GET | `/api/state` | Existing UI state, active history, busy status, completed records |

Only one pipeline/history operation can run at a time. Concurrent submissions return **409**; the API does not queue or automatically reset histories. Keep one Reactor history per dataset history. `history_id` refers to Reactor's history, not the dataset's ID. Keep dataset IDs in `context`. A restart marks unfinished runs failed without replaying prompts that might already have reached Reactor.

### Submit input

```json
{
  "original_prompt": "When is the wedding?",
  "question_id": "q_example0001",
  "question_date": "2023/05/30 (Tue) 08:08",
  "evidence": [
    {"id":"s_example0002", "date":"2023/05/27 (Sat) 10:05",
     "text":"user: Update: the wedding moved to June, same hall."}
  ],
  "context": {"dataset_history_id":"h_example0001"},
  "reference": {"answer":"June", "aliases":[], "abstain":false, "evidence":["s_example0002"]},
  "dry_run": true
}
```

Save this JSON as `request.json` and submit:

```sh
curl -sS http://127.0.0.1:8873/api/v1/runs \
  -H "Authorization: Bearer $HALO_API_TOKEN" \
  -H 'Content-Type: application/json' --data-binary @request.json
```

Response:

```json
{"run_id":"run_<32 hex characters>","status":"queued","poll_url":"/api/v1/runs/run_<32 hex characters>"}
```

Poll `poll_url` until status is **prepared**, **completed**, or **failed**. Other states are `queued`, `preparing`, `running`.

- `dry_run: true` invokes the configured harness and builds the exact message, but never calls Reactor. It needs no Reactor key or history and produces no model score.
- `dry_run: false` sends the prepared message to Reactor. It requires a Reactor key and history.
- Optional `history_id` overrides the current saved Reactor history without resetting it.
- `reactor_key` may be supplied in the request body if the server has no `REACTOR_ACCESS_KEY`. It is excluded from the stored input and harness payload. Prefer the server environment.
- `reference` is optional. Missing reference scores are `null`, not zero. Gold is used only after inference. Do not put gold answers or credentials in `context` or evidence.
- The fully compiled message, including instructions, must fit 16,000 characters. The harness can shorten/retrieve evidence before this check.

## Harness callback contract

The dashboard POSTs the following to your registered URL:

```json
{
  "contract_version": "halo.harness.v1",
  "run_id": "run_…",
  "question_id": "q_example0001",
  "original_prompt": "When is the wedding?",
  "question_date": "2023/05/30 (Tue) 08:08",
  "evidence": [],
  "context": {"dataset_history_id":"h_example0001"}
}
```

Return HTTP 200 with:

```json
{
  "contract_version": "halo.harness.v1",
  "prompt": "When is the wedding? Include citations in the final answer.",
  "question_date": "2023/05/30 (Tue) 08:08",
  "evidence": [{"id":"s_example0002","date":"2023/05/27 (Sat) 10:05","text":"The wedding moved to June."}]
}
```

`prompt` and `contract_version` are required. Omitted `evidence` or `question_date` inherit the original input; `evidence: []` explicitly clears evidence. Each evidence item requires a nonempty `id` and `text`, with an optional date. Other response fields are ignored. The response is limited to 100 KB, and timeout is configurable from 1 to 120 seconds. Failures, invalid responses, and oversized messages **fail closed**: no fallback model request is sent.

Replace `prepare()` in [examples/harness.py](examples/harness.py) with your own indexing/retrieval, prompt rewriting, reranking, or preprocessing. No Reactor credentials or reference answers are passed to it.

## Evaluation

Persisted records live in ignored `results/runs/run_<id>.json` files, written atomically. Completed executions appear in the dashboard; Diagnostics includes the pipeline stages and exact outgoing message. Dry runs are available through the API and preview dialog, not mixed into model-response metrics.

Per-run metrics:

- `completed`: whether Reactor returned a marked final answer.
- `abstained`, `citation_count`: observed model behavior.
- `exact_match`: normalized answer/alias comparison on answerable reference questions. This is not semantic judging.
- `evidence_f1`: session-ID overlap on answerable reference questions when reference evidence is supplied.
- `abstention_correct`: correct answer/abstain decision against the reference.

Failed executed attempts score false/zero when a reference is supplied. Unanswerable references have `exact_match` and `evidence_f1` set to null. Aggregates report both value and denominator and treat repeated question IDs as distinct attempts. Dataset-wide missing questions are not inferred by this per-run API; the dashboard's gold-file evaluation remains available for that use case.

Attach gold later without another model request:

```sh
curl -sS http://127.0.0.1:8873/api/v1/runs/RUN_ID/evaluate \
  -H "Authorization: Bearer $HALO_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"reference":{"answer":"June","abstain":false,"evidence":["s_example0002"]}}'
```

## Validation

```sh
node dashboard/test_metrics.cjs
.venv/bin/python dashboard/test_server.py
```

Integration tests start a real local HTTP harness and dashboard API, but mock Reactor. They check transformations, exact payload capture, credential exclusion, gold isolation, dry runs, scoring, late evaluation, authentication, validation, concurrency rejection, and failure behavior.
