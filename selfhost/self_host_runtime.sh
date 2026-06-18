#!/usr/bin/env bash
# Shared self-host runtime ownership, locking, and health helpers.
#
# Expects self_host_env.sh to be sourced first (or UNITY_HOME / SELF_HOST_STATE_DIR set).

set -euo pipefail

SELF_HOST_RUNTIME_OWNER_SERVICE="service"
SELF_HOST_RUNTIME_OWNER_STACK="stack"

self_host_runtime_state_file() {
  printf '%s/runtime-state.json' "${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}"
}

self_host_runtime_lock_file() {
  printf '%s/runtime.lock' "${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}"
}

self_host_service_marker_file() {
  printf '%s/service-enabled' "${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}"
}

self_host_service_supervisor_pidfile() {
  printf '%s/service-supervisor.pid' "${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}"
}

self_host_service_log_file() {
  printf '%s/service.log' "${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}"
}

self_host_service_is_enabled() {
  [[ -f "$(self_host_service_marker_file)" ]]
}

self_host_enable_runtime() {
  self_host_ensure_state_dir
  touch "$(self_host_service_marker_file)"
}

self_host_disable_runtime() {
  rm -f "$(self_host_service_marker_file)"
}

self_host_ensure_state_dir() {
  mkdir -p "${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}"
}

self_host_read_runtime_state() {
  local state_file
  state_file="$(self_host_runtime_state_file)"
  if [[ ! -f "$state_file" ]]; then
    return 1
  fi
  python3 - "$state_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
for key in (
    "owner",
    "pid",
    "assistant_id",
    "gateway_owner",
    "gateway_pid",
):
    print(data.get(key, "") or "")
PY
}

self_host_runtime_gateway_owner() {
  self_host_read_runtime_state 2>/dev/null | sed -n '4p' || true
}

self_host_runtime_gateway_pid() {
  self_host_read_runtime_state 2>/dev/null | sed -n '5p' || true
}

self_host_gateway_base_url() {
  printf 'http://%s:%s' \
    "${UNITY_GATEWAY_HOST:-127.0.0.1}" \
    "${UNITY_GATEWAY_PORT:-8001}"
}

self_host_gateway_pidfile() {
  printf '/tmp/unity-gateway.pid'
}

self_host_gateway_process_pid() {
  local pidfile
  pidfile="$(self_host_gateway_pidfile)"
  [[ -f "$pidfile" ]] || return 1
  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null || return 1
  printf '%s' "$pid"
}

self_host_gateway_process_is_running() {
  self_host_gateway_process_pid >/dev/null 2>&1
}

self_host_gateway_is_healthy() {
  self_host_gateway_process_is_running || return 1
  command -v curl >/dev/null 2>&1 || return 0
  curl -sf "$(self_host_gateway_base_url)/health" >/dev/null 2>&1
}

self_host_patch_runtime_state() {
  self_host_ensure_state_dir
  python3 - "$(self_host_runtime_state_file)" "$@" <<'PY'
import json
import sys
from datetime import datetime, timezone

path = sys.argv[1]
updates = {}
for arg in sys.argv[2:]:
    key, _, value = arg.partition("=")
    if not key:
        continue
    updates[key] = value

data: dict = {}
try:
    with open(path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    if isinstance(loaded, dict):
        data = loaded
except FileNotFoundError:
    pass
except json.JSONDecodeError:
    pass

for key, value in updates.items():
    if value == "":
        data.pop(key, None)
    else:
        data[key] = value

data["updated_at"] = datetime.now(timezone.utc).isoformat()
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
PY
}

self_host_write_runtime_state() {
  local owner="$1"
  local pid="$2"
  local assistant_id="$3"
  self_host_patch_runtime_state \
    "owner=$owner" \
    "pid=$pid" \
    "assistant_id=$assistant_id"
}

self_host_write_gateway_state() {
  local owner="$1"
  local pid="$2"
  self_host_patch_runtime_state \
    "gateway_owner=$owner" \
    "gateway_pid=$pid"
}

self_host_clear_runtime_state() {
  rm -f "$(self_host_runtime_state_file)"
}

unity_cm_pidfile() {
  printf '/tmp/unity-local.pid'
}

unity_cm_process_pids() {
  local main_pids="" pidfile_pid="" merged=""
  main_pids="$(pgrep -f "[u]nity\.conversation_manager\.main" 2>/dev/null || true)"
  if [[ -f "$(unity_cm_pidfile)" ]]; then
    pidfile_pid="$(cat "$(unity_cm_pidfile)" 2>/dev/null || true)"
    if [[ -n "$pidfile_pid" ]] && ! kill -0 "$pidfile_pid" 2>/dev/null; then
      pidfile_pid=""
    fi
  fi
  merged="$(printf '%s\n%s' "$main_pids" "$pidfile_pid" | sed '/^$/d' | sort -u)"
  if [[ -n "$merged" ]]; then
    printf '%s\n' "$merged"
  fi
}

unity_cm_instance_count() {
  local pids count
  pids="$(unity_cm_process_pids)"
  if [[ -z "$pids" ]]; then
    echo 0
    return 0
  fi
  count="$(printf '%s\n' "$pids" | sed '/^$/d' | wc -l | tr -d ' ')"
  echo "$count"
}

unity_cm_assistant_id_for_pid() {
  local pid="$1"
  ps eww -p "$pid" 2>/dev/null \
    | tr ' ' '\n' \
    | sed -n 's/^ASSISTANT_ID=//p' \
    | head -1
}

unity_cm_is_alive() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

self_host_runtime_owner_for_pid() {
  local pid="$1"
  local owner=""
  if [[ -f "$(self_host_runtime_state_file)" ]]; then
    local state_pid state_owner
    state_pid="$(self_host_read_runtime_state 2>/dev/null | sed -n '2p' || true)"
    state_owner="$(self_host_read_runtime_state 2>/dev/null | sed -n '1p' || true)"
    if [[ "$state_pid" == "$pid" && -n "$state_owner" ]]; then
      printf '%s' "$state_owner"
      return 0
    fi
  fi
  printf ''
}

self_host_clear_service_supervisor_pidfile() {
  rm -f "$(self_host_service_supervisor_pidfile)"
}

self_host_service_supervisor_process_command() {
  local pid="${1:-}"
  [[ -n "$pid" ]] || return 1
  ps -p "$pid" -o args= 2>/dev/null \
    || ps -p "$pid" -o command= 2>/dev/null \
    || true
}

self_host_pid_is_service_supervisor() {
  local pid="${1:-}"
  local cmd=""
  cmd="$(self_host_service_supervisor_process_command "$pid")"
  [[ -n "$cmd" ]] || return 1
  [[ "$cmd" == *"service.sh"* && "$cmd" == *" run"* ]]
}

self_host_find_service_supervisor_pid() {
  local pid=""
  pid="$(pgrep -f "[s]ervice\.sh run" 2>/dev/null | head -1 || true)"
  if [[ -n "$pid" ]] && self_host_pid_is_service_supervisor "$pid"; then
    printf '%s' "$pid"
    return 0
  fi
  return 1
}

self_host_repair_service_supervisor_pidfile() {
  local pid=""
  pid="$(self_host_find_service_supervisor_pid 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  self_host_ensure_state_dir
  printf '%s' "$pid" >"$(self_host_service_supervisor_pidfile)"
  return 0
}

self_host_service_supervisor_is_running() {
  local pidfile pid
  pidfile="$(self_host_service_supervisor_pidfile)"
  if [[ -f "$pidfile" ]]; then
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null \
      && self_host_pid_is_service_supervisor "$pid"; then
      return 0
    fi
    self_host_clear_service_supervisor_pidfile
  fi
  self_host_repair_service_supervisor_pidfile
}

self_host_headless_scheduling_ready() {
  self_host_should_preserve_background_on_interactive_stop
}

self_host_ensure_service_supervisor() {
  local service_script="${1:-}"
  if ! self_host_service_is_enabled; then
    return 0
  fi
  if self_host_service_supervisor_is_running; then
    return 0
  fi
  self_host_clear_service_supervisor_pidfile
  if [[ -z "$service_script" || ! -f "$service_script" ]]; then
    return 1
  fi
  bash "$service_script" start
}

self_host_service_runtime_is_healthy() {
  self_host_service_is_enabled || return 1
  self_host_service_supervisor_is_running || return 1
  local count
  count="$(unity_cm_instance_count)"
  [[ "$count" -eq 1 ]]
}

self_host_should_preserve_background_on_interactive_stop() {
  self_host_service_is_enabled || return 1
  self_host_service_supervisor_is_running
}

self_host_should_preserve_runtime_on_interactive_stop() {
  self_host_should_preserve_background_on_interactive_stop || return 1
  local count
  count="$(unity_cm_instance_count)"
  [[ "$count" -eq 1 ]]
}

self_host_should_preserve_orchestra_on_interactive_stop() {
  self_host_should_preserve_background_on_interactive_stop
}

self_host_should_preserve_gateway_on_interactive_stop() {
  self_host_service_is_enabled || return 1
  self_host_service_supervisor_is_running || return 1
  self_host_gateway_is_healthy
}

self_host_service_supervisor_should_run() {
  self_host_service_is_enabled \
    && ! self_host_service_supervisor_is_running
}

self_host_service_supervisor_pid() {
  if ! self_host_service_supervisor_is_running; then
    return 1
  fi
  cat "$(self_host_service_supervisor_pidfile)"
}

self_host_adopt_coordinator_for_service() {
  local coordinator_agent_id="${1:-}"
  local cm_pid=""

  cm_pid="$(cat "$(unity_cm_pidfile)" 2>/dev/null || true)"
  [[ -n "$cm_pid" ]] || return 1
  unity_cm_is_alive "$cm_pid" || return 1

  if [[ -z "$coordinator_agent_id" ]]; then
    coordinator_agent_id="$(unity_cm_assistant_id_for_pid "$cm_pid")"
  fi
  [[ -n "$coordinator_agent_id" ]] || return 1

  self_host_write_runtime_state \
    "$SELF_HOST_RUNTIME_OWNER_SERVICE" \
    "$cm_pid" \
    "$coordinator_agent_id"
}

self_host_apply_service_coordinator_context() {
  if ! self_host_service_is_enabled; then
    return 0
  fi
  if ! self_host_service_supervisor_is_running; then
    return 0
  fi
  export UNITY_RUNTIME_OWNER="$SELF_HOST_RUNTIME_OWNER_SERVICE"
  export UNITY_SERVICE_RUNTIME=1
}

with_unity_runtime_start_lock() {
  local timeout="${1:-30}"
  shift
  self_host_ensure_state_dir
  local lock_file
  lock_file="$(self_host_runtime_lock_file)"
  python3 - "$lock_file" "$timeout" "$@" <<'PY'
import fcntl
import os
import subprocess
import sys
import time

lock_path = sys.argv[1]
timeout_s = float(sys.argv[2])
cmd = sys.argv[3:]
os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
with open(lock_path, "w") as lock_fp:
    deadline = time.time() + timeout_s
    while True:
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() >= deadline:
                sys.exit(2)
            time.sleep(0.2)
    raise SystemExit(subprocess.call(cmd))
PY
}

self_host_runtime_doctor_line() {
  local service_label="not installed"
  if self_host_service_is_enabled; then
    if self_host_service_supervisor_is_running; then
      service_label="running"
    else
      service_label="stopped"
    fi
  fi

  local cm_count
  cm_count="$(unity_cm_instance_count)"
  local cm_label
  if [[ "$cm_count" -eq 0 ]]; then
    cm_label="0 instances (stopped)"
  elif [[ "$cm_count" -eq 1 ]]; then
    cm_label="1 instance (ok)"
  else
    cm_label="${cm_count} instances (ERROR — split brain risk)"
  fi

  printf 'service: %s\n' "$service_label"
  printf 'CM: %s\n' "$cm_label"
  if self_host_gateway_process_is_running; then
    printf 'gateway: running (%s)\n' "$(self_host_gateway_base_url)"
  else
    printf 'gateway: stopped (%s)\n' "$(self_host_gateway_base_url)"
  fi
}

self_host_load_coordinator_credentials() {
  local runtime_file="${1:-${SELF_HOST_COORDINATOR_RUNTIME_FILE:-}}"
  if [[ -z "$runtime_file" ]]; then
    if declare -F self_host_coordinator_runtime_file &>/dev/null; then
      runtime_file="$(self_host_coordinator_runtime_file)"
    else
      runtime_file="${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}/coordinator-runtime.json"
    fi
  fi
  if [[ ! -f "$runtime_file" ]]; then
    return 1
  fi
  python3 - "$runtime_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
print(data.get("api_key") or data.get("apiKey") or "")
print(data.get("coordinator_agent_id") or data.get("coordinatorAgentId") or "")
PY
}
