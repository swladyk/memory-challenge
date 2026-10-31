#!/usr/bin/env bash
# Live run on a scorable dev slice: 40 dev-large lockbox questions (never used for tuning; 4 unanswerable, the other
# 36 spread over the five question types), one Reactor history per question, on team hAlo's agent t05. Answers are
# graded locally as they arrive (live view); afterwards scripts/score_dev_eval_run.sh scores them with score.py and
# scripts/compare_dev_runs.sh compares the two systems.
#
#   bash scripts/start_dev_eval_run.sh          # our solution (configs/baseline.toml), slice in runs/dev-live
#   bash scripts/start_dev_eval_run.sh kit      # the GitHub kit's keyword baseline (run_benchmark.py's retrieve(),
#                                               # configs/a-kit.toml) on the same questions, in runs/dev-live-kit
#
# Each slice folder holds selection.json and gold.subset.jsonl. The access key comes from the
# private file written by scripts/save_access_key.sh or, without it, from a hidden prompt; it is cleaned, checked for
# the rxk_ prefix, kept only in this process's environment and never printed.
set -u
cd "$(dirname "$0")/.." || exit 1
KEY_FILE="${REACTOR_KEY_FILE:-$HOME/.config/halo-reactor/t05_access_key}"
case "${1:-ours}" in
    ours) RUN=runs/dev-live; CONFIG=configs/baseline.toml; WHAT="our solution" ;;
    kit) RUN=runs/dev-live-kit; CONFIG=configs/a-kit.toml; WHAT="the GitHub kit's keyword baseline" ;;
    *) echo "usage: bash scripts/start_dev_eval_run.sh [ours|kit]"; exit 2 ;;
esac

export PYTHONPATH=src
export REACTOR_BASE_URL=https://t05.api.reactor-hackathon.rxai.dev
export REACTOR_AGENT_IDS=agt_afcb0e0cb62f62db

if pgrep -f "memory_challenge run-reactor" >/dev/null; then
    echo "A Reactor run is already going on this Mac. Not starting a second one."
    exit 1
fi
if [ ! -d "$RUN/payloads" ] || [ ! -f "$RUN/gold.subset.jsonl" ]; then
    echo "Missing $RUN/payloads or $RUN/gold.subset.jsonl: the dev slice has not been prepared."
    exit 1
fi

echo "Team hAlo · agent t05 · $WHAT · dev-large lockbox slice: 40 questions, 40 histories · about 60-80 minutes."
echo "Make sure no teammate is using agent t05."
if [ -f "$KEY_FILE" ]; then
    mode=$(stat -f '%Lp' "$KEY_FILE" 2>/dev/null || stat -c '%a' "$KEY_FILE")
    if [ "$mode" != "600" ] && [ "$mode" != "400" ]; then
        echo "$KEY_FILE can be read by others (mode $mode). Run  chmod 600 $KEY_FILE  and start again."
        exit 1
    fi
    raw_key=$(<"$KEY_FILE")
    echo "Using the access key saved in $KEY_FILE (scripts/save_access_key.sh)."
else
    printf 'Paste the Reactor access key (starts with rxk_; nothing will show), then press Enter: '
    IFS= read -rs raw_key
    echo
fi
REACTOR_ACCESS_KEY=$(printf '%s' "$raw_key" | tr -d "\033\"' \t\r\n" | sed 's/\[20[01]~//g')
unset raw_key
case "$REACTOR_ACCESS_KEY" in
    rxk_?*) ;;
    *) echo "That doesn't look like the access key (it should start with rxk_). Nothing was sent."
       unset REACTOR_ACCESS_KEY
       exit 1 ;;
esac
export REACTOR_ACCESS_KEY
echo "Key accepted. Starting the run: keep this window open until it finishes."

caffeinate -i .venv/bin/python -m memory_challenge run-reactor --payloads "$RUN" --config "$CONFIG" \
    --backend live --allow-live --gold "$RUN/gold.subset.jsonl"
status=$?
unset REACTOR_ACCESS_KEY
echo "Run finished with exit code $status. Score it with:  bash scripts/score_dev_eval_run.sh ${1:-ours}"
exit $status
