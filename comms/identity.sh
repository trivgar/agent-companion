#!/usr/bin/env bash
# Who is speaking in this session. A session's name is stored by its session
# key (the claude process that runs it) in the roster.
#
#   identity.sh set <Name>   claim a name from the cast
#   identity.sh free [Name]  release yours (or the named one)
#   identity.sh get          your name; claims the first free one if you have none
#   identity.sh cast         the names, their Piper voices, and whether each is taken
#   identity.sh roster       who holds what
set -u
# shellcheck source=_env.sh
. "$(dirname "${BASH_SOURCE[0]}")/_env.sh"
ROSTER="$STATE/roster.tsv"
touch "$ROSTER"
exec 8>"$STATE/roster.lock"; flock 8

session_key() {
  if [[ -n "${TTS_SESSION_KEY:-}" ]]; then echo "$TTS_SESSION_KEY"; return; fi
  local p="$PPID" n=0 cmd start
  while [[ -n "$p" && "$p" != 0 && "$p" != 1 && $n -lt 12 ]]; do
    cmd=$(ps -o args= -p "$p" 2>/dev/null | head -1 | awk '{print $1}')
    if [[ -n "$cmd" && "$(basename "$cmd")" == claude ]]; then
      start=$(awk '{print $22}' "/proc/$p/stat" 2>/dev/null)
      echo "p${p}.${start:-0}"; return
    fi
    p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    n=$((n+1))
  done
  echo "p$PPID.0"
}

alive() {   # a key whose process is gone holds nothing
  if [[ "$1" =~ ^p([0-9]+)\. ]]; then [[ -d "/proc/${BASH_REMATCH[1]}" ]]; else return 0; fi
}

canon() { awk -F'\t' -v n="$1" 'tolower($1)==tolower(n){print $1; exit}' "$CAST"; }

holder() {  # live key holding a name, if any
  local k nm
  while IFS=$'\t' read -r k nm _; do
    [[ "$nm" == "$1" ]] && alive "$k" && { echo "$k"; return; }
  done < "$ROSTER"
}

drop_key() { grep -v -P "^\Q$1\E\t" "$ROSTER" > "$ROSTER.new"; mv "$ROSTER.new" "$ROSTER"; }
drop_name() { awk -F'\t' -v n="$1" '$2!=n' "$ROSTER" > "$ROSTER.new"; mv "$ROSTER.new" "$ROSTER"; }
mine() { awk -F'\t' -v k="$1" '$1==k{print $2; exit}' "$ROSTER"; }

claim() {   # key name
  drop_key "$1"; drop_name "$2"
  printf '%s\t%s\t%s\n' "$1" "$2" "$(date +%s)" >> "$ROSTER"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  set)
    name=$(canon "${1:-}")
    [[ -z "$name" ]] && { echo "identity: '${1:-}' is not in the cast ($(cut -f1 "$CAST" | tr '\n' ' '))" >&2; exit 1; }
    key=$(session_key); h=$(holder "$name")
    if [[ -n "$h" && "$h" != "$key" ]]; then echo "identity: $name is taken" >&2; exit 1; fi
    claim "$key" "$name"; echo "$name" ;;
  free)
    if [[ -n "${1:-}" ]]; then name=$(canon "$1"); [[ -n "$name" ]] && drop_name "$name"
    else drop_key "$(session_key)"; fi ;;
  get)
    [[ -n "${COMMS_JOB:-}${AGENT_COMPANION_JOB:-}" ]] && exit 0
    key=$(session_key); name=$(mine "$key")
    if [[ -z "$name" ]]; then
      while IFS=$'\t' read -r n _; do
        [[ -z "$(holder "$n")" ]] && { name="$n"; break; }
      done < "$CAST"
      [[ -z "$name" ]] && name=$(head -1 "$CAST" | cut -f1)
      claim "$key" "$name"
    fi
    echo "$name" ;;
  cast)
    while IFS=$'\t' read -r n v; do
      [[ -z "$n" ]] && continue
      if [[ -n "$(holder "$n")" ]]; then s=taken; else s=free; fi
      printf '%s\t%s\t%s\n' "$n" "$v" "$s"
    done < "$CAST" ;;
  roster)
    while IFS=$'\t' read -r k nm t; do
      alive "$k" && printf '%s\t%s\t%s\n' "$k" "$nm" "$t"
    done < "$ROSTER" ;;
  *) echo "usage: identity.sh set|free|get|cast|roster" >&2; exit 2 ;;
esac
