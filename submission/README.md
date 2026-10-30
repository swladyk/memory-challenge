# hAlo submission workflow

Run from the repository root (or `code/` inside the submission archive). Python 3.11+.
Reactor produces every submitted answer. No full test predictions are included in this source checkout.

## Setup and connect the final harness

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r reactor/requirements.txt
```

Start the dashboard with `HALO_API_TOKEN` and `REACTOR_ACCESS_KEY` in its environment;
see [the API guide](../dashboard/API.md) for secure interactive entry and harness configuration.
Use the same `HALO_API_TOKEN` in the runner terminal. Never commit keys.
The final harness receives `context.dataset_history_id`, question/date and evidence.
It must load/index the corresponding dataset locally and return dated excerpts with
session IDs as `id`. It never receives gold answers. Keep question dates unchanged.

## Plan without calling Reactor

Replace dataset paths below with the event's downloaded files.

```sh
.venv/bin/python submission/workflow.py plan --questions test/questions.jsonl --histories test/histories.jsonl
```

## Run when the final harness is ready

```sh
.venv/bin/python submission/workflow.py run \
  --questions test/questions.jsonl --histories test/histories.jsonl \
  --evidence-mode none --harness-version YOUR_HARNESS_COMMIT --out results/submission
```

`none` requires an enabled harness that retrieves its own evidence. For the included
lexical baseline use `--evidence-mode bm25` and disable the external harness.
The baseline searches both conversation roles, excludes future sessions, and budgets
dated snippets to 12,000 characters before the API's 16,000-character limit.
It is a baseline, not a claim of benchmark quality.

The run resets Reactor between dataset histories, sends questions sequentially and
can take hours. Use a dedicated agent/server during a run. Repeating the identical
command skips completed histories. An interrupted history restarts in full with new
memory; failed calls never become fabricated abstentions. Changed datasets, workflow,
client or harness configuration require a new output directory. Version your harness
and keep its dependencies and indexing instructions with its source.

## Export and validate

A successful run creates `predictions.jsonl`, raw run records, a checkpoint and a
validation report. Export verifies full coverage, Reactor final-answer provenance,
exact outgoing messages, dates, and session ownership. Only citation labels are
removed from Reactor's answer. The client's insufficient-evidence marker becomes
`abstain: true`; evidence citations are retained even on abstentions.

```sh
.venv/bin/python submission/workflow.py export --questions test/questions.jsonl --histories test/histories.jsonl --checkpoint results/submission/checkpoint.json --out results/submission/predictions.jsonl
.venv/bin/python submission/workflow.py validate --questions test/questions.jsonl --histories test/histories.jsonl --predictions results/submission/predictions.jsonl
```

## Dev scoring

Run the workflow on dev questions/histories into a separate output directory, then:

```sh
python3 score.py dev/gold.jsonl results/dev/predictions.jsonl --details results/dev/scores.jsonl
```

`score.py` is the upstream standard-library scorer. It reports each question type
and overall: locally matched answer fraction, unjudged fraction, evidence F1,
abstention recall on unanswerable questions and false abstention on answerable ones.
Evidence F1 includes every answerable question, even when the prediction abstains.
Unmatched answers remain **unjudged**, not automatically wrong. Its phrase matcher
can accept hedging or negation that the official LLM judge rejects: local matches
are not official accuracy. Submit one concise answer, not alternative guesses.
The dashboard's exact-match diagnostic is separate from this benchmark scorer.
No combined presentation/benchmark score is assumed before kickoff rules are known.

## Package all three deliverables

```sh
.venv/bin/python submission/workflow.py package \
  --questions test/questions.jsonl --histories test/histories.jsonl \
  --predictions results/submission/predictions.jsonl \
  --checkpoint results/submission/checkpoint.json \
  --harness-source /path/to/final-harness \
  --presentation submission/presentation.md --out results/submission.zip
```

The archive contains predictions, code with run instructions, your presentation,
source hashes and recorded runs for reproducibility. A final harness requires its
source directory; include its dependency manifest and setup instructions there.
The presentation is a short editable talk outline; replace it with the kickoff's
required format using `--presentation`. Update measured dev results after running.
The archive contains conversation excerpts in its run records; distribute it through
the event submission channel. The public repository needs only source, not run data.
No dataset, credentials, virtual environment or results directory is added as code.

Public source: https://github.com/swladyk/memory-challenge/tree/front

## Verification

```sh
.venv/bin/python -m unittest discover -s submission -p 'test_*.py'
.venv/bin/python dashboard/test_server.py
node dashboard/test_metrics.cjs
```
