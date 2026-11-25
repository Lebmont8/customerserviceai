#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
LOG="$ROOT/watchdog.log"
LOCK=/tmp/customerserviceai-watchdog.lock
MAIN="$ROOT/main.py"
CLIENT="$ROOT/client_bot.py"
RUNNER="$ROOT/log_runner.py"
RUN="$ROOT/ops/run.sh"
HEALTH="$ROOT/healthcheck.py"
START_TIMEOUT=45

ts() { date '+%F %T'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG"; }
log_ok() { [ "${QUIET_OK:-0}" = "1" ] || log "$@"; }

exec 9>"$LOCK"
if ! flock -n 9; then
  log "another watchdog run is active; skipping"
  exit 0
fi

process_count() {
  pgrep -fc "^/usr/bin/python3 $1$" || true
}

stop_local_service() {
  pkill -TERM -f "^/usr/bin/python3 $RUNNER$" 2>/dev/null || true
  deadline=$(( $(date +%s) + 12 ))
  while [ "$(process_count "$RUNNER")" -gt 0 ] && [ "$(date +%s)" -lt "$deadline" ]; do
    sleep 1
  done
  pkill -TERM -f "^/usr/bin/python3 $MAIN$" 2>/dev/null || true
  pkill -TERM -f "^/usr/bin/python3 $CLIENT( |$)" 2>/dev/null || true
}

runner_count=$(process_count "$RUNNER")
main_count=$(process_count "$MAIN")
client_count=$(pgrep -fc "^/usr/bin/python3 $CLIENT( |$)" || true)
health_status=0
python3 "$HEALTH" >/dev/null 2>&1 || health_status=$?
if [ "$health_status" -eq 0 ] && [ "$runner_count" -eq 1 ]; then
  log_ok "ok: runner=$runner_count main=$main_count client=$client_count"
  exit 0
fi
if [ "$health_status" -eq 2 ]; then
  log "blocked: Telegram polling conflict; manual resolution required"
  stop_local_service
  exit 1
fi

log "restart needed: runner=$runner_count main=$main_count client=$client_count"
stop_local_service

setsid "$RUN" 9>&- >/dev/null 2>&1 < /dev/null &
deadline=$(( $(date +%s) + START_TIMEOUT ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  sleep 1
  runner_count=$(process_count "$RUNNER")
  main_count=$(process_count "$MAIN")
  client_count=$(pgrep -fc "^/usr/bin/python3 $CLIENT( |$)" || true)
  health_status=0
  python3 "$HEALTH" >/dev/null 2>&1 || health_status=$?
  if [ "$health_status" -eq 0 ] && [ "$runner_count" -eq 1 ]; then
    log "started: runner=$runner_count main=$main_count client=$client_count"
    exit 0
  fi
  if [ "$health_status" -eq 2 ]; then
    log "blocked after start: Telegram polling conflict; stopping local processes"
    stop_local_service
    exit 1
  fi
done

log "failed after restart: runner=$runner_count main=$main_count client=$client_count health=$health_status"
exit 1
