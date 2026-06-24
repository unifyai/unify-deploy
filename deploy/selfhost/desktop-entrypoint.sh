#!/usr/bin/env bash
# Self-host desktop: use the coordinator owner's API key for VNC and agent-service.
set -euo pipefail

RUNTIME_FILE="${SELF_HOST_COORDINATOR_RUNTIME_FILE:-/runtime/coordinator-runtime.json}"
POLL_SECONDS="${SELF_HOST_DESKTOP_POLL_SECONDS:-2}"

log() { echo "[unity-desktop] $*"; }

read_api_key() {
  python3 - "$RUNTIME_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
print(data.get("apiKey") or data.get("api_key") or "")
PY
}

while [[ ! -f "$RUNTIME_FILE" ]]; do
  log "Waiting for coordinator runtime at ${RUNTIME_FILE}..."
  sleep "$POLL_SECONDS"
done

while true; do
  api_key="$(read_api_key || true)"
  if [[ -n "$api_key" ]]; then
    export UNIFY_KEY="$api_key"
    break
  fi
  log "Runtime file present but missing apiKey; retrying..."
  sleep "$POLL_SECONDS"
done

export UNITY_GATEWAY_URL="${UNITY_GATEWAY_URL:-http://gateway:8001}"
export ORCHESTRA_URL="${ORCHESTRA_URL:-http://orchestra:8000/v0}"
export UNITY_COMMS_URL="${UNITY_COMMS_URL:-$UNITY_GATEWAY_URL}"

update_agent_service_env() {
  local env_file="/app/agent-service/.env"
  if [[ ! -f "$env_file" ]]; then
    log "agent-service .env not found at ${env_file}; skipping env sync"
    return 0
  fi
  python3 - "$env_file" "$UNIFY_KEY" "$ORCHESTRA_URL" "$UNITY_COMMS_URL" "$UNITY_GATEWAY_URL" "${UNIFY_MODEL:-}" <<'PY'
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
unify_key, orchestra_url, comms_url, gateway_url, unify_model = sys.argv[2:7]
keys = {
    "UNIFY_KEY": unify_key,
    "ORCHESTRA_URL": orchestra_url,
    "UNITY_COMMS_URL": comms_url,
    "UNITY_GATEWAY_URL": gateway_url,
}
if unify_model:
    keys["UNIFY_MODEL"] = unify_model
    keys["UNITY_AGENT_SERVICE_LLM_MODEL"] = unify_model
lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
for key, value in keys.items():
    prefix = f"{key}="
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{prefix}{value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{prefix}{value}")
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
  log "Synced coordinator credentials into agent-service .env"
}

update_agent_service_env

ensure_playwright_cache_for_unityuser() {
  local root_cache="/root/.cache/ms-playwright"
  local user_cache="/Unity/.cache/ms-playwright"
  if [[ ! -d "$root_cache" ]]; then
    return 0
  fi
  if [[ -L "$user_cache" ]] || [[ ! -r "$user_cache/chromium_headless_shell-1223/chrome-linux/headless_shell" ]]; then
    log "Seeding Playwright browser cache for unityuser..."
    rm -rf "$user_cache"
    cp -a "$root_cache" "$user_cache"
    chown -R unityuser:unityuser "$user_cache"
  fi
}

ensure_chromium_browser_wrapper() {
  local user_cache="/Unity/.cache/ms-playwright"
  local chrome_path
  chrome_path="$(find "$user_cache" -name "chrome" -type f -executable 2>/dev/null | head -1 || true)"
  if [[ -z "$chrome_path" ]]; then
    log "Playwright chrome binary not found under ${user_cache}; skipping browser wrapper update"
    return 0
  fi
  printf '#!/bin/bash\nexec "%s" --no-sandbox "$@"\n' "$chrome_path" > /usr/local/bin/chromium-browser
  chmod +x /usr/local/bin/chromium-browser
}

ensure_playwright_cache_for_unityuser
ensure_chromium_browser_wrapper

log "Using coordinator API key for VNC and agent-service auth"
exec /bin/bash /app/desktop/startup.sh
