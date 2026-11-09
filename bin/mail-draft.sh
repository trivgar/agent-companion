#!/usr/bin/env bash
#
# mail-draft.sh -- read what arrived, queue replies that want one, send nothing.
# Run by the user timer in systemd/mail-draft.timer. The work is in
# `python -m agent_companion.mail draft`; its claude runs are allowed the mail
# provider's READ tools only (agent_companion/mail/gmail.py), and the writing
# step holds no mail tool at all.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${AGENT_COMPANION_PYTHON:-python3}"
LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/agent-companion"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/mail.log"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

out=$("$PY" -m agent_companion.mail draft 2>&1)
rc=$?
log "draft rc=$rc :: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)"

# Backstop: approvals whose draft was not made when the page asked.
"$ROOT/bin/mail-approve.sh" </dev/null >/dev/null 2>&1 || true
exit 0          # a failed run must not stop the timer
