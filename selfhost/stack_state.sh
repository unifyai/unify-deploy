#!/usr/bin/env bash
# Shared source/compose stack state and guard helpers.

set -euo pipefail

stack_state_dir() {
  printf '%s' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

stack_state_file() {
  printf '%s/full-stack-state.json' "$(stack_state_dir)"
}

stack_state_ensure_dir() {
  mkdir -p "$(stack_state_dir)"
}

stack_state_lsof_listeners() {
  local ports="${1:-3000,8000,8001,8085,8090}"
  command -v lsof >/dev/null 2>&1 || return 0
  local args=()
  local port
  IFS=',' read -r -a _stack_ports <<<"$ports"
  for port in "${_stack_ports[@]}"; do
    [[ -n "$port" ]] && args+=("-iTCP:$port")
  done
  if [[ ${#args[@]} -eq 0 ]]; then
    return 0
  fi
  lsof -nP "${args[@]}" -sTCP:LISTEN 2>/dev/null || true
}

stack_state_port_is_listening() {
  local port="$1"
  command -v lsof >/dev/null 2>&1 || return 1
  lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sed -n '2p' | grep -q .
}

stack_state_write_source() {
  stack_state_ensure_dir
  python3 - "$(stack_state_file)" <<'PY'
import json
import os
import socket
import sys
from datetime import datetime, timezone

path = sys.argv[1]

def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)

data = {
    "mode": "source",
    "updated_at": datetime.now(timezone.utc).isoformat(),
    "host": socket.gethostname(),
    "repo_paths": {
        "unify_deploy": env("DEPLOY_REPO_PATH"),
        "unity": env("UNIFY_REPO_PATH"),
        "console": env("CONSOLE_REPO_PATH"),
        "orchestra": env("ORCHESTRA_REPO_PATH"),
    },
    "ports": {
        "console": env("CONSOLE_PORT", "3000"),
        "orchestra": env("ORCHESTRA_PORT", "8000"),
        "gateway": env("UNIFY_GATEWAY_PORT", "8001"),
        "pubsub": env("PUBSUB_EMULATOR_PORT", "8085"),
        "desktop": "8090",
    },
    "console_env": {
        "SELF_HOST": "1",
        "NEXT_PUBLIC_SELF_HOST": "1",
        "NEXT_PUBLIC_CONSOLE_DEBUG": "true",
        "NEXTAUTH_URL": f"http://localhost:{env('CONSOLE_PORT', '3000')}",
        "ORCHESTRA_URL": f"http://127.0.0.1:{env('ORCHESTRA_PORT', '8000')}",
        "LOCAL_ADAPTERS_URL": env("LOCAL_ADAPTERS_URL", f"http://127.0.0.1:{env('UNIFY_GATEWAY_PORT', '8001')}"),
        "UNIFY_ADAPTERS_URL": env("UNIFY_ADAPTERS_URL", f"http://127.0.0.1:{env('UNIFY_GATEWAY_PORT', '8001')}"),
        "COMMUNICATION_URL": env("COMMUNICATION_URL", f"http://127.0.0.1:{env('UNIFY_GATEWAY_PORT', '8001')}"),
        "PUBSUB_EMULATOR_HOST": env("PUBSUB_EMULATOR_HOST", "localhost:8085"),
        "GCP_PROJECT_ID": env("GCP_PROJECT_ID", "local-test-project"),
        "PUBSUB_TOPIC_SUFFIX": env("PUBSUB_TOPIC_SUFFIX", "-staging"),
        "LIVEKIT_URL": env("LIVEKIT_URL"),
        "SELF_HOST_DESKTOP_URL": env("SELF_HOST_DESKTOP_URL", "http://127.0.0.1:8090"),
    },
}

with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
PY
}

stack_state_mode() {
  local file
  file="$(stack_state_file)"
  [[ -f "$file" ]] || return 1
  python3 - "$file" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print(json.load(fh).get("mode", ""))
except Exception:
    print("")
PY
}

stack_state_print_console_env() {
  local file
  file="$(stack_state_file)"
  if [[ ! -f "$file" ]]; then
    echo "No full-stack state manifest found at $file"
    return 1
  fi
  python3 - "$file" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
for key, value in data.get("console_env", {}).items():
    print(f"{key}={value}")
PY
}

stack_state_source_is_active() {
  local mode
  mode="$(stack_state_mode 2>/dev/null || true)"
  if [[ -n "$mode" && "$mode" != "source" ]]; then
    return 1
  fi
  stack_state_port_is_listening 8000 && stack_state_port_is_listening 8001
}

stack_state_compose_is_active() {
  command -v docker >/dev/null 2>&1 || return 1
  docker ps --filter "label=com.docker.compose.project=unity-selfhost" --format '{{.Names}}' 2>/dev/null \
    | grep -q .
}

stack_state_refuse_if_compose_active() {
  if [[ "${UNIFY_ALLOW_STACK_MODE_MIX:-0}" == "1" ]]; then
    return 0
  fi
  if stack_state_compose_is_active; then
    echo "[ERROR] Docker Compose self-host is already running (project unity-selfhost)." >&2
    echo "Use the compose-backed unity CLI, or stop it before starting source mode:" >&2
    echo "  unity down --full" >&2
    echo "Override only if you understand the port/process collision risk:" >&2
    echo "  UNIFY_ALLOW_STACK_MODE_MIX=1 $0 up" >&2
    return 1
  fi
}

stack_state_refuse_if_source_active() {
  if [[ "${UNIFY_ALLOW_STACK_MODE_MIX:-0}" == "1" ]]; then
    return 0
  fi
  if stack_state_source_is_active; then
    echo "[ERROR] Source self-host stack appears to be running." >&2
    echo "Use unity-deploy/selfhost/stack.sh status/repair-console, or stop it first:" >&2
    echo "  bash /Users/djl11/unity-deploy/selfhost/stack.sh down --full" >&2
    echo "Override only if you understand the port/process collision risk:" >&2
    echo "  UNIFY_ALLOW_STACK_MODE_MIX=1 unity up" >&2
    return 1
  fi
}
