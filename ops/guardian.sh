#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LOG="$ROOT/guardian.log"
ENSURE="$ROOT/ops/ensure.sh"
LOCK=/tmp/customerserviceai-guardian.lock
PIDFILE=/tmp/customerserviceai-guardian.pid

umask 077
if ! mkdir "$LOCK" 2>/dev/null; then
  printf '[%s] guardian already running; skipping\n' "$(date '+%F %T')" >> "$LOG"
  exit 0
fi
printf '%s\n' "$$" > "$PIDFILE"
trap 'rm -f "$PIDFILE"; rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM
printf '[%s] guardian started\n' "$(date '+%F %T')" >> "$LOG"

while :; do
  if ! QUIET_OK=1 "$ENSURE"; then
    printf '[%s] ensure failed\n' "$(date '+%F %T')" >> "$LOG"
  fi
  sleep 60
done
