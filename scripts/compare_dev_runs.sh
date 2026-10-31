#!/usr/bin/env bash
# Compare the GitHub kit's keyword baseline (reference) with our solution on the same 40 dev-large lockbox questions,
# using score.py's own functions: per-type scores side by side, per-question gains and losses, 95% intervals, and
# what each system sent (gold sessions in the evidence). Contacts nothing.
#
#   bash scripts/compare_dev_runs.sh [RUN_NAME]   # default reactor-live; e.g. reactor-fake for the offline dry run
#
# Score each finished run first (bash scripts/score_dev_eval_run.sh ours / kit). A system without predictions yet is
# compared on what it sent only, so this also works before any Reactor run.
set -u
cd "$(dirname "$0")/.." || exit 1
NAME="${1:-reactor-live}"
export PYTHONPATH=src
args=(--gold runs/dev-live/gold.subset.jsonl --reference kit --payloads kit=runs/dev-live-kit --payloads ours=runs/dev-live)
for pair in kit=runs/dev-live-kit ours=runs/dev-live; do
    pred="${pair#*=}/predictions.$NAME.jsonl"
    if [ -f "$pred" ]; then args+=(--system "${pair%%=*}=$pred"); else echo "(no $pred yet: ${pair%%=*} is compared on what it sent only)"; fi
done
.venv/bin/python -m memory_challenge compare "${args[@]}" --out "runs/dev-compare/$NAME"
