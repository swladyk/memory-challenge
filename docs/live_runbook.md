# Live run runbook: dev smoke test, then the test split

Nothing here has been run yet. Every step that calls Reactor needs the user's go-ahead. Run the commands in **your
own terminal** from the `memory-challenge` folder, on branch `feat/retrieval-framework`.

Budget (organisers, 2026-10-30 and 2026-10-31): one agent per team, questions strictly one at a time. Latency grows
as questions accumulate in one Reactor history: another team saw ~60 s at first, up to ~200 s later, and an occasional
question past 300 s (several internal agent cycles, no server error). Each test history has ~10 questions and gets a
fresh Reactor history, so latency starts low again with each one. At 60–200 s per answer the 146 test questions take
**~3–6 h**; each question that times out adds up to 10 min (the 600 s client wait) plus a drain ping and, if its
answer never shows up, a re-ask. Plan **~10–15 min** for the smoke test. **The deadline is today: start the test run
as early as possible.** An interrupted run still exports everything answered so far, and resumes where it stopped.

**Only one person may use the team's agent (t05) at a time**, and every new history resets its memory. A second user
gets `busy` refusals or takes over the history (`history_changed`; our run then stops rather than reset theirs).

This is runner A in `docs/integration.md`; the team-pipeline alternative (B) is at the end.

## 0. Prerequisites (you provide or approve)

| What | How |
| --- | --- |
| **Agent t05 is free** | **Ask the teammate who shares t05 (or the team channel) and wait for a clear "it's yours" before every live step.** Nobody else may use t05 meanwhile: not the team dashboard's Send, `reactor/smoke_test.py`, `submission/workflow.py run`, or a second `run-reactor`. Tell them when you are done |
| Project-local install of `websockets>=14` and `certifi` (`reactor/requirements.txt`) | `python3 -m venv .venv && .venv/bin/python -m pip install -r reactor/requirements.txt` (Python 3.11+). **Done** in this checkout: websockets 17.4, certifi 2026.10.23 |
| Team base URL and agent id (not secret; `reactor/LOCAL_SETUP.md`) | `export REACTOR_BASE_URL=https://t05.api.reactor-hackathon.rxai.dev REACTOR_AGENT_IDS=agt_afcb0e0cb62f62db` |
| Access key | typed by you at a hidden prompt in that terminal, never in chat, a file or a command line: `printf 'Access key: '; read -rs REACTOR_ACCESS_KEY; echo; export REACTOR_ACCESS_KEY` |
| Keep the Mac awake during the run | prefix the long command with `caffeinate -i` (built into macOS) |

Then, in the same terminal:

```bash
export PYTHONPATH=src
```

The key stays in that terminal's environment. Our code never reads, logs or passes it: `reactor/client.py`
reads it from the environment it inherits.

## 1. Smoke test on dev (5 questions, ~10 min)

Five dev questions covering a user fact, an assistant recommendation, a knowledge update, a temporal question and an
unanswerable one. Retrieval for these was checked offline: gold evidence is packed for the four answerable ones.

```bash
.venv/bin/python -m memory_challenge retrieve --split dev --config configs/baseline.toml \
    --question-id q_8196f8a0ee6f --question-id q_322888af981d --question-id q_6863ec3c6b22 \
    --question-id q_584d0cd7ca07 --question-id q_1773242b1801 --out runs/smoke-dev
.venv/bin/python -m memory_challenge run-reactor --payloads runs/smoke-dev --config configs/baseline.toml \
    --backend live --allow-live --gold dev/gold.jsonl
.venv/bin/python -m memory_challenge export-predictions --run runs/smoke-dev/reactor-live --payloads runs/smoke-dev \
    --histories dev/histories.jsonl --questions dev/questions.jsonl --out runs/smoke-dev/predictions.jsonl
.venv/bin/python -m memory_challenge validate-predictions --predictions runs/smoke-dev/predictions.jsonl \
    --questions dev/questions.jsonl --histories dev/histories.jsonl --gold dev/gold.jsonl
```

Check before going on:

- `run-reactor` reports 5 outcomes, ideally `answered` / `reactor_abstained`. Any `error` on the first question
  usually means a wrong URL, agent id or key (`reactor/PARTICIPANTS.md`, Troubleshooting).
- Latency is roughly 60–200 s per question: the Live evaluation view (`docs/framework.md`, "Live logs") plots it
  per request, in order, or see `reactor_latency_s` in `runs/smoke-dev/reactor-live/attempts.jsonl`.
- `export-predictions` prints `citation_fallback`: how many answers had no `[E#]` label and how many were filled.
  The file `runs/smoke-dev/predictions.jsonl.citation_fallback.json` shows each decision.
- The score table is over all 526 dev questions, so 521 count as missing. Look only at whether the 5 answers make
  sense: the hamster question (`q_1773242b1801`) should abstain.

## 2. Test run (146 questions, 15 histories, ~3–6 h)

Check again that nobody else is using t05.

```bash
.venv/bin/python -m memory_challenge retrieve --split test --config configs/baseline.toml --out runs/test-live
caffeinate -i .venv/bin/python -m memory_challenge run-reactor --payloads runs/test-live --config configs/baseline.toml \
    --backend live --allow-live
```

- One new Reactor history per test history (15), the questions in file order, never reset between them.
- Progress: open **Live evaluation** in the team dashboard (`docs/framework.md`, "Live logs"). Its live view only
  reads `runs/`; don't send questions from the dashboard's Conversations view during the run. Or, from a second
  terminal (no key needed):

```bash
python3 -c "import json,collections; a=[json.loads(l) for l in open('runs/test-live/reactor-live/attempts.jsonl')]; print(collections.Counter(x['outcome'] for x in a if x.get('kind')=='ask'), 'asked', len({x['question_id'] for x in a if x.get('kind')=='ask'}), '/ 146')"
```

- **Interrupted?** Run the same `run-reactor` command again (after checking t05 is still free). Questions with a
  final answer are skipped. The history you were in continues in **its stored Reactor history** (the log keeps the
  id), and late answers to its earlier timeouts can still be collected there. A new Reactor history is started only
  if the server answers `history_changed` (the agent has moved on since). The reason is logged as a `new_history`
  record in `attempts.jsonl`. Each payload carries its own evidence, so nothing needs reloading.
- **Timeouts** follow the organisers' advice. The client waits 600 s (`reactor.timeout_s`; `--timeout` to change it),
  and a timeout does not cancel the question on Reactor's side. So the run moves on to the next question. That
  request waits until the previous one has cleared, and brings back the late answer, which becomes the timed-out
  question's result (`answered_late`, cited against its own passages). When a history's questions are done, a
  question still without an answer gets a short ping ("Reply only OK.") to collect late answers. Then it is re-asked
  once in the **same** Reactor history (`--reask 0` to skip that). A new history is never started in the middle of a
  dataset history.
- `previous_pending` / `busy` refusals are retried by the client, then up to 3 more times 7 s apart.

## 3. Export and validate the submission

```bash
mkdir -p submission
.venv/bin/python -m memory_challenge export-predictions --run runs/test-live/reactor-live --payloads runs/test-live \
    --histories test/histories.jsonl --questions test/questions.jsonl --out submission/predictions.jsonl
.venv/bin/python -m memory_challenge validate-predictions --predictions submission/predictions.jsonl \
    --questions test/questions.jsonl --histories test/histories.jsonl
```

- `export-predictions` runs the kit's `reactor/predictions.py` (through the session-id file that avoids its U+2028
  bug) with `--abstain-on agent`: Reactor's `INSUFFICIENT_EVIDENCE` becomes `abstain: true`. A question that timed
  out stays **missing** and is listed under `skipped`. Missing questions score as wrong either way; deciding to
  submit them as abstentions instead (`--abstain-on agent,no-answer`) is the team's call.
- The supported-citation fallback fills citations only where a packed passage supports Reactor's answer
  (`docs/framework.md`, "Supported-citation fallback"). Answers are never edited.
- `validate-predictions` must report `"valid": true`, ideally with 146 predictions and 0 missing.

`submission/predictions.jsonl` is the file to submit, together with the code and a short README
(`README.md:85-99`). Keep `runs/test-live/` (payloads, attempts log, manifest) as evidence that the predictions came
from running this code. It is gitignored because it contains conversation text.

## Alternative B: team pipeline with this harness

Same evidence, through the dashboard API (`dashboard/API.md`) and `submission/workflow.py`. The workflow stops at
the first failed question, and an interrupted history is rerun in full (`docs/integration.md`).

```bash
# terminal 1: our harness for the test split
PYTHONPATH=src .venv/bin/python -m memory_challenge harness --split test --port 8890 --out runs/harness-test
# terminal 2: the dashboard (it holds the key); hidden prompts as in dashboard/API.md
read -rs 'HALO_API_TOKEN?Choose a local API token: '; export HALO_API_TOKEN
read -rs 'REACTOR_ACCESS_KEY?Reactor access key: '; export REACTOR_ACCESS_KEY
HARNESS_URL=http://127.0.0.1:8890/prepare .venv/bin/python dashboard/server.py
# terminal 3: the run (same HALO_API_TOKEN), then export with the fallback
caffeinate -i .venv/bin/python submission/workflow.py run --questions test/questions.jsonl \
    --histories test/histories.jsonl --evidence-mode none --harness-version "$(git rev-parse --short HEAD)" \
    --citation-fallback --out results/submission
.venv/bin/python submission/workflow.py export --questions test/questions.jsonl --histories test/histories.jsonl \
    --checkpoint results/submission/checkpoint.json --citation-fallback --out results/submission/predictions.jsonl
```

`workflow.py package ... --citation-fallback --harness-source src` builds the team's archive.
