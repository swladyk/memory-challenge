#!/usr/bin/env bash
# Start the live test run: 146 test questions, one Reactor history per test history, on team hAlo's agent t05.
#
#   bash scripts/start_test_run.sh
#
# The access key comes from the private file written by scripts/save_access_key.sh (outside the repository, owner-only)
# or, without it, from a hidden prompt. Either way it is cleaned of quotes, spaces and paste markers, checked for the
# rxk_ prefix, kept only in this process's environment for the run, and never printed.
set -u
KEY_FILE="${REACTOR_KEY_FILE:-$HOME/.config/halo-reactor/t05_access_key}"
cd "$(dirname "$0")/.." || exit 1

export PYTHONPATH=src
export REACTOR_BASE_URL=https://t05.api.reactor-hackathon.rxai.dev
export REACTOR_AGENT_IDS=agt_afcb0e0cb62f62db

if pgrep -f "memory_challenge run-reactor" >/dev/null; then
    echo "A test run is already going on this Mac. Not starting a second one."
    exit 1
fi
if [ ! -x .venv/bin/python ]; then
    echo "Missing .venv: run  python3 -m venv .venv && .venv/bin/python -m pip install -r reactor/requirements.txt"
    exit 1
fi
if [ ! -d runs/test-live/payloads ]; then
    echo "Building the test evidence (about 10 seconds)..."
    .venv/bin/python -m memory_challenge retrieve --split test --config configs/baseline.toml --out runs/test-live || exit 1
fi

echo "Team hAlo · agent t05 · 146 test questions · about 3-6 hours. Make sure no teammate is using agent t05."
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
    *) echo "That doesn't look like the access key (it should start with rxk_). Nothing was sent; run the script again."
       unset REACTOR_ACCESS_KEY
       exit 1 ;;
esac
export REACTOR_ACCESS_KEY
echo "Key accepted (${#REACTOR_ACCESS_KEY} characters). Starting the run: keep this window open until it finishes."

caffeinate -i .venv/bin/python -m memory_challenge run-reactor --payloads runs/test-live --config configs/baseline.toml \
    --backend live --allow-live
status=$?
unset REACTOR_ACCESS_KEY
echo "Run finished with exit code $status. Results: runs/test-live/reactor-live/"
exit $status
