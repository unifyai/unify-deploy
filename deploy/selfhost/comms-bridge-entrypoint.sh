#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
READY_FILE="${SELF_HOST_CALL_CONTROLLER_READY_FILE:-/runtime/call-controller-ready}"
BRIDGE_READY_FILE="${SELF_HOST_COMMS_BRIDGE_READY_FILE:-/runtime/comms-bridge-ready}"
POLL_SECONDS="${SELF_HOST_COMMS_BRIDGE_READY_POLL_SECONDS:-2}"
LEASE_MAX_AGE_SECONDS="${SELF_HOST_CALL_LEASE_MAX_AGE_SECONDS:-180}"
rm -f "$BRIDGE_READY_FILE"
# shellcheck source=load-comms-secrets.sh
source "$SCRIPT_DIR/load-comms-secrets.sh"

calls_enabled() {
  local value="${SELF_HOST_INTERNAL_CALLS_ENABLED:-false}"
  case "${value,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

lease_is_current() {
  [[ -s "$READY_FILE" ]] || return 1
  local heartbeat now
  heartbeat="$(<"$READY_FILE")"
  [[ "$heartbeat" =~ ^[0-9]+$ ]] || return 1
  now="$(date +%s)"
  (( now - heartbeat <= LEASE_MAX_AGE_SECONDS ))
}

wait_for_cm() {
  local health_url="${COMMS_BRIDGE_INGRESS_URL%/}/local/comms/health"
  echo "[comms-bridge] waiting for CM local ingress"
  until python3 -c \
    'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2)' \
    "$health_url" >/dev/null 2>&1; do
    sleep "$POLL_SECONDS"
  done
}

if ! calls_enabled; then
  unset TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN
  unset TWILIO_WA_ACCOUNT_SID TWILIO_WA_AUTH_TOKEN
fi

acquired=false
bridge_pid=""
stopping=false

shutdown() {
  stopping=true
  rm -f "$BRIDGE_READY_FILE"
  if [[ -n "$bridge_pid" ]]; then
    kill "$bridge_pid" 2>/dev/null || true
    wait "$bridge_pid" 2>/dev/null || true
  fi
  if [[ "$acquired" == "true" ]] \
    && ! python3 "$SCRIPT_DIR/sync_comms_webhooks.py" --release-text; then
    exit 1
  fi
  exit 0
}

cleanup_on_exit() {
  local status=$?
  rm -f "$BRIDGE_READY_FILE"
  if [[ "$stopping" != "true" && "$acquired" == "true" ]]; then
    if [[ -n "$bridge_pid" ]]; then
      kill "$bridge_pid" 2>/dev/null || true
      wait "$bridge_pid" 2>/dev/null || true
    fi
    python3 "$SCRIPT_DIR/sync_comms_webhooks.py" --release-text || status=1
  fi
  exit "$status"
}

trap shutdown TERM INT
trap cleanup_on_exit EXIT

wait_for_cm
if calls_enabled; then
  echo "[comms-bridge] waiting for call ownership lease"
  sleep "$POLL_SECONDS"
  while ! lease_is_current; do
    sleep "$POLL_SECONDS"
  done
fi

if calls_enabled; then
  # Mark acquisition as attempted before the mutating command so a partial
  # provider update is compare-and-restored if reconciliation fails.
  acquired=true
  python3 "$SCRIPT_DIR/sync_comms_webhooks.py" --acquire-text
fi
python3 "$SCRIPT_DIR/comms_ingress_bridge.py" &
bridge_pid=$!
printf 'ready\n' >"$BRIDGE_READY_FILE"
chmod 0600 "$BRIDGE_READY_FILE"

while kill -0 "$bridge_pid" 2>/dev/null; do
  if calls_enabled && ! lease_is_current; then
    echo "[comms-bridge] call ownership lease lost" >&2
    exit 1
  fi
  sleep "$POLL_SECONDS"
done
wait "$bridge_pid"
