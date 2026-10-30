# Live run runbook: dev smoke test, then the test split

Nothing here has been run yet. Every step that calls Reactor needs the user's go-ahead. Run the commands in **your
own terminal** from the `memory-challenge` folder, on branch `feat/retrieval-framework`.

Budget (organisers, 2026-10-30): one agent per team, questions strictly one at a time, ~50–60 s per answer plus
15–30 s of internal cycle, ~2.5 h or more for the 146 test questions. Plan **~10 min** for the smoke test and
**2.5–3.5 h** for the test run. Only one person may use the team's agent at a time: a second user gets `busy`
refusals or takes over the history (`history_changed`).

## 0. Prerequisites (you provide or approve)

| What | How |
| --- | --- |
| Project-local install of `websockets>=14` (the only dependency of `reactor/client.py`) | `python3 -m venv .venv && .venv/bin/pip install -e '.[reactor]'` (Python 3.11+) |
| Team base URL and agent id (not secret, from the event handout) | `export REACTOR_BASE_URL=https://tNN.api.reactor-hackathon.rxai.dev REACTOR_AGENT_IDS=agt_...` (one id) |
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
    --backend live --allow-live
.venv/bin/python -m memory_challenge export-predictions --run runs/smoke-dev/reactor-live --payloads runs/smoke-dev \
    --histories dev/histories.jsonl --questions dev/questions.jsonl --out runs/smoke-dev/predictions.jsonl
.venv/bin/python -m memory_challenge validate-predictions --predictions runs/smoke-dev/predictions.jsonl \
    --questions dev/questions.jsonl --histories dev/histories.jsonl --gold dev/gold.jsonl
```

Check before going on:

- `run-reactor` reports 5 outcomes, ideally `answered` / `reactor_abstained`. Any `error` on the first question
  usually means a wrong URL, agent id or key (`reactor/PARTICIPANTS.md`, Troubleshooting).
- Latency is roughly 60–90 s per question (the `reactor_latency_s` field in `runs/smoke-dev/reactor-live/attempts.jsonl`).
- `export-predictions` prints `citation_fallback`: how many answers had no `[E#]` label and how many were filled.
  The file `runs/smoke-dev/predictions.jsonl.citation_fallback.json` shows each decision.
- The score table is over all 526 dev questions, so 521 count as missing. Look only at whether the 5 answers make
  sense: the hamster question (`q_1773242b1801`) should abstain.

## 2. Test run (146 questions, 15 histories, 2.5–3.5 h)

```bash
.venv/bin/python -m memory_challenge retrieve --split test --config configs/baseline.toml --out runs/test-live
caffeinate -i .venv/bin/python -m memory_challenge run-reactor --payloads runs/test-live --config configs/baseline.toml \
    --backend live --allow-live
```

- One new Reactor history per test history (15), the questions in file order, never reset between them.
- Progress, from a second terminal (no key needed):

```bash
python3 -c "import json,collections; a=[json.loads(l) for l in open('runs/test-live/reactor-live/attempts.jsonl')]; print(collections.Counter(x['outcome'] for x in a if x.get('kind')=='ask'), 'asked', len({x['question_id'] for x in a if x.get('kind')=='ask'}), '/ 146')"
```

- **Interrupted?** Run the same `run-reactor` command again. Questions with a final answer are skipped. Histories
  with questions left start a fresh Reactor history; each payload carries its own evidence, so nothing needs
  reloading.
- **Timeouts or closed chats** (`ambiguous`) are not retried automatically: the question may have reached the agent.
  After the run, if time allows, re-ask only those with
  `... run-reactor --payloads runs/test-live --config configs/baseline.toml --backend live --allow-live --retry-ambiguous`.
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
