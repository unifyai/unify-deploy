#!/usr/bin/env bash
# Wait for the compose desktop proxy and coordinator runtime, then publish
# assistant_desktop_ready so Console unlocks liveview / remote control.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_FILE="${SELF_HOST_COORDINATOR_RUNTIME_FILE:-/runtime/coordinator-runtime.json}"
POLL_SECONDS="${SELF_HOST_DESKTOP_READY_POLL_SECONDS:-3}"
INTERNAL_URL="${SELF_HOST_DESKTOP_INTERNAL_URL:-http://desktop-proxy:8090}"
BROWSER_URL="${SELF_HOST_DESKTOP_URL:-http://127.0.0.1:8090}"
# The python helper ships alongside this script in the self-host bundle
# (overlaid into the image at /app/deploy/selfhost/).
PUBLISH_SCRIPT="${SELF_HOST_DESKTOP_READY_SCRIPT:-$SCRIPT_DIR/publish_self_host_desktop_ready.py}"

log() { echo "[desktop-ready] $*"; }

wait_for_runtime() {
  while [[ ! -f "$RUNTIME_FILE" ]]; do
    log "Waiting for coordinator runtime at ${RUNTIME_FILE}..."
    sleep "$POLL_SECONDS"
  done
}

read_agent_id() {
  python3 - "$RUNTIME_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
print(data.get("coordinatorAgentId") or data.get("coordinator_agent_id") or "")
PY
}

desktop_healthy() {
  python3 - "$INTERNAL_URL" <<'PY'
import sys
import urllib.error
import urllib.request

base = sys.argv[1].rstrip("/")
probe = f"{base}/desktop/vnc.html"
try:
    with urllib.request.urlopen(probe, timeout=5) as resp:
        print(resp.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
except Exception:
    print("000")
PY
}

is_healthy() {
  local code="$1"
  [[ "$code" != "000" && "$code" -lt 500 ]]
}

wait_for_desktop() {
  while true; do
    local code
    code="$(desktop_healthy || echo "000")"
    if is_healthy "$code"; then
      log "Desktop proxy is healthy (GET /desktop/vnc.html -> ${code})"
      return 0
    fi
    log "Waiting for desktop at ${INTERNAL_URL} (last status ${code})..."
    sleep "$POLL_SECONDS"
  done
}

publish_ready() {
  local agent_id="$1"
  python3 "$PUBLISH_SCRIPT" \
    --assistant-id "$agent_id" \
    --desktop-url "$BROWSER_URL"
}

last_published=""

while true; do
  wait_for_runtime
  agent_id="$(read_agent_id)"
  if [[ -z "$agent_id" ]]; then
    log "Runtime file present but missing coordinator agent id; retrying..."
    sleep "$POLL_SECONDS"
    continue
  fi

  wait_for_desktop

  if [[ "$last_published" != "$agent_id" ]]; then
    log "Publishing assistant_desktop_ready for assistant ${agent_id}..."
    if publish_ready "$agent_id"; then
      last_published="$agent_id"
      log "Published desktop ready at ${BROWSER_URL}"
    else
      log "Publish failed; will retry..."
      sleep "$POLL_SECONDS"
      continue
    fi
  fi

  sleep "$POLL_SECONDS"
  next_agent_id="$(read_agent_id)"
  if [[ -n "$next_agent_id" && "$next_agent_id" != "$last_published" ]]; then
    last_published=""
  fi
done
