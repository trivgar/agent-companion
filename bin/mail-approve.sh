#!/usr/bin/env bash
#
# mail-approve.sh -- turn approved replies into DRAFTS in the owner's mail account.
# Drafts, never sends: the owner opens the draft and sends it themselves. The
# approving Claude run is allowed only ToolSearch and create_draft, and nothing
# else (agent_companion/mail/gmail.py). Idempotent: a reply that already has a
# draft id is skipped, and a lock keeps two passes from drafting the same reply.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${AGENT_COMPANION_PYTHON:-python3}"
LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}/agent-companion"
mkdir -p "$LOGDIR"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
out=$("$PY" -m agent_companion.mail approve 2>&1)
rc=$?
printf '%s approve rc=%s :: %s\n' "$(date '+%F %T')" "$rc" "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)" >> "$LOGDIR/mail.log"
exit "$rc"
