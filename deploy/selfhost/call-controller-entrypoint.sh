#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TUNNEL_LOG="${SELF_HOST_CALL_TUNNEL_LOG:-/runtime/call-tunnel.log}"
TUNNEL_URL_FILE="${SELF_HOST_CALL_TUNNEL_URL_FILE:-/runtime/call-tunnel-url}"
READY_FILE="${SELF_HOST_CALL_CONTROLLER_READY_FILE:-/runtime/call-controller-ready}"
POLL_SECONDS="${SELF_HOST_CALL_CONTROLLER_POLL_SECONDS:-2}"
LEASE_CHECK_SECONDS="${SELF_HOST_CALL_LEASE_CHECK_SECONDS:-30}"
LEASE_TRANSIENT_FAILURE_LIMIT="${SELF_HOST_CALL_LEASE_TRANSIENT_FAILURE_LIMIT:-3}"
RELEASE_RETRY_SECONDS="${SELF_HOST_CALL_RELEASE_RETRY_SECONDS:-300}"
CM_HEALTH_URL="${SELF_HOST_CM_HEALTH_URL:-http://unity-cm:8787/local/comms/health}"

# shellcheck source=load-comms-secrets.sh
source "$SCRIPT_DIR/load-comms-secrets.sh"

case "${1:-}" in
  --release|--release-text|--release-voice)
    exec python3 "$SCRIPT_DIR/sync_comms_webhooks.py" "$1"
    ;;
esac

: "${TWILIO_ACCOUNT_SID:?internal calls require TWILIO_ACCOUNT_SID}"
: "${TWILIO_AUTH_TOKEN:?internal calls require TWILIO_AUTH_TOKEN}"
: "${TWILIO_WA_ACCOUNT_SID:?internal calls require TWILIO_WA_ACCOUNT_SID}"
: "${TWILIO_WA_AUTH_TOKEN:?internal calls require TWILIO_WA_AUTH_TOKEN}"
: "${LIVEKIT_URL:?internal calls require LIVEKIT_URL}"
: "${LIVEKIT_API_KEY:?internal calls require LIVEKIT_API_KEY}"
: "${LIVEKIT_API_SECRET:?internal calls require LIVEKIT_API_SECRET}"
: "${LIVEKIT_SIP_URI:?internal calls require LIVEKIT_SIP_URI}"

rm -f "$READY_FILE"
acquired=false
stopping=false

release_voice() {
  [[ "$acquired" == "true" ]] || return 0
  python3 "$SCRIPT_DIR/sync_comms_webhooks.py" --release-voice
}

shutdown() {
  stopping=true
  rm -f "$READY_FILE"
  if release_voice; then
    rm -f "$TUNNEL_URL_FILE"
  else
    exit 1
  fi
  exit 0
}

cleanup_on_exit() {
  local status=$?
  rm -f "$READY_FILE"
  if [[ "$stopping" != "true" && "$acquired" == "true" ]]; then
    if release_voice; then
      rm -f "$TUNNEL_URL_FILE"
    else
      status=1
    fi
  fi
  exit "$status"
}

trap shutdown TERM INT
trap cleanup_on_exit EXIT

echo "[call-controller] waiting for CM local ingress"
until python3 -c \
  'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2)' \
  "$CM_HEALTH_URL" >/dev/null 2>&1; do
  sleep "$POLL_SECONDS"
done

latest_tunnel_url() {
  [[ -f "$TUNNEL_LOG" ]] || return 1
  grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" \
    | tail -1
}

tunnel_is_reachable() {
  python3 - "$1" <<'PY'
import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen(sys.argv[1], timeout=5)
except urllib.error.HTTPError as exc:
    if exc.code != 404:
        raise SystemExit(1)
except (urllib.error.URLError, OSError):
    raise SystemExit(1)
else:
    raise SystemExit(1)
PY
}

publish_tunnel_url() {
  local url="$1"
  local temporary="${TUNNEL_URL_FILE}.$$"
  printf '%s\n' "$url" >"$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$TUNNEL_URL_FILE"
}

publish_ready() {
  local temporary="${READY_FILE}.$$"
  date +%s >"$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$READY_FILE"
}

configure_calls() {
  local public_url="$1"
  python3 "$SCRIPT_DIR/provision_call_sip.py"
  acquired=true
  publish_tunnel_url "$public_url"
  UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL="$public_url" \
    python3 "$SCRIPT_DIR/sync_comms_webhooks.py" --set-voice-only
  publish_ready
}

current_url=""
current_log_signature=""
last_lease_check=0
lease_transient_failures=0
lease_confirmed=false
lease_blocked=false
last_release_attempt=0
while true; do
  next_log_signature="$(
    stat -c '%Y:%s' "$TUNNEL_LOG" 2>/dev/null \
      || stat -f '%m:%z' "$TUNNEL_LOG" 2>/dev/null \
      || true
  )"
  if [[ -n "$next_log_signature" \
    && "$next_log_signature" != "$current_log_signature" ]]; then
    next_url="$(latest_tunnel_url || true)"
    current_log_signature="$next_log_signature"
    if [[ "$lease_blocked" != "true" \
      && -n "$next_url" \
      && "$next_url" != "$current_url" ]] \
      && tunnel_is_reachable "$next_url"; then
      set +e
      configure_calls "$next_url"
      configure_status=$?
      set -e
      if (( configure_status == 0 )); then
        current_url="$next_url"
        last_lease_check="$(date +%s)"
        lease_transient_failures=0
        lease_confirmed=true
        echo "[call-controller] call edge ready at $current_url"
      else
        rm -f "$READY_FILE"
        current_log_signature=""
        lease_confirmed=false
        echo "[call-controller] call edge acquisition failed; retrying" >&2
        sleep "$LEASE_CHECK_SECONDS"
      fi
    fi
  fi
  now="$(date +%s)"
  if [[ "$lease_blocked" != "true" && -n "$current_url" ]] \
    && (( now - last_lease_check >= LEASE_CHECK_SECONDS )); then
    set +e
    UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL="$current_url" \
      python3 "$SCRIPT_DIR/sync_comms_webhooks.py" --set-voice-only --check
    lease_status=$?
    set -e
    case "$lease_status" in
      0)
        lease_transient_failures=0
        lease_confirmed=true
        publish_tunnel_url "$current_url"
        ;;
      1)
        rm -f "$READY_FILE" "$TUNNEL_URL_FILE"
        lease_confirmed=false
        echo "[call-controller] remote call ownership lease lost" >&2
        if release_voice; then
          acquired=false
        fi
        last_release_attempt="$now"
        lease_blocked=true
        ;;
      *)
        lease_transient_failures=$((lease_transient_failures + 1))
        echo "[call-controller] lease check unavailable (${lease_transient_failures}/${LEASE_TRANSIENT_FAILURE_LIMIT})" >&2
        if (( lease_transient_failures >= LEASE_TRANSIENT_FAILURE_LIMIT )); then
          rm -f "$READY_FILE" "$TUNNEL_URL_FILE"
          lease_confirmed=false
        fi
        ;;
    esac
    last_lease_check="$now"
  fi
  if [[ "$lease_blocked" == "true" && "$acquired" == "true" ]] \
    && (( now - last_release_attempt >= RELEASE_RETRY_SECONDS )); then
    if release_voice; then
      acquired=false
      echo "[call-controller] remaining owned call callbacks released"
    else
      echo "[call-controller] owned callback release still blocked; retrying later" >&2
    fi
    last_release_attempt="$now"
  fi
  if [[ "$lease_blocked" != "true" \
    && -n "$current_url" \
    && "$lease_confirmed" == "true" ]] \
    && (( lease_transient_failures < LEASE_TRANSIENT_FAILURE_LIMIT )); then
    publish_ready
  fi
  sleep "$POLL_SECONDS"
done
