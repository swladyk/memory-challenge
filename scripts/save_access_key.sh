#!/usr/bin/env bash
# Save team hAlo's Reactor access key once, so scripts/start_test_run.sh reads it instead of asking every time.
#
#   bash scripts/save_access_key.sh
#
# The key is read at a hidden prompt (never on screen, on a command line or in shell history), cleaned and checked the
# way start_test_run.sh does, and written to a private file OUTSIDE the repository, readable only by you (chmod 600),
# so it can never be committed (reactor/PARTICIPANTS.md, step 2). Another location: set REACTOR_KEY_FILE.
# To remove it:  rm ~/.config/halo-reactor/t05_access_key
set -u
KEY_FILE="${REACTOR_KEY_FILE:-$HOME/.config/halo-reactor/t05_access_key}"

printf 'Paste the Reactor access key (starts with rxk_; nothing will show), then press Enter: '
IFS= read -rs raw_key
echo
key=$(printf '%s' "$raw_key" | tr -d "\033\"' \t\r\n" | sed 's/\[20[01]~//g')
unset raw_key
case "$key" in
    rxk_?*) ;;
    *) echo "That doesn't look like the access key (it should start with rxk_). Nothing was saved."
       unset key
       exit 1 ;;
esac

umask 077
mkdir -p "$(dirname "$KEY_FILE")" && chmod 700 "$(dirname "$KEY_FILE")" || exit 1
printf '%s' "$key" > "$KEY_FILE.tmp" && chmod 600 "$KEY_FILE.tmp" && mv "$KEY_FILE.tmp" "$KEY_FILE" || exit 1
echo "Saved (${#key} characters) to $KEY_FILE, readable only by you. start_test_run.sh will use it from now on."
unset key
