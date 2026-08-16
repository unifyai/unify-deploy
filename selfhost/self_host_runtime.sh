#!/usr/bin/env bash
# Shared self-host runtime ownership, locking, and health helpers.
#
# Expects self_host_env.sh to be sourced first (or UNIFY_HOME / SELF_HOST_STATE_DIR set).

set -euo pipefail

SELF_HOST_RUNTIME_OWNER_SERVICE="service"
SELF_HOST_RUNTIME_OWNER_STACK="stack"

# Canonical packaged helpers live under deploy/selfhost so source and Compose
# installs execute the same implementation.
_SELF_HOST_RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
_SELF_HOST_PACKAGED_DIR="${SELF_HOST_DEPLOY_SELFHOST_DIR:-$_SELF_HOST_RUNTIME_DIR/../deploy/selfhost}"

self_host_runtime_state_file() {
  printf '%s/runtime-state.json' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_runtime_lock_file() {
  printf '%s/runtime.lock' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_service_marker_file() {
  printf '%s/service-enabled' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_service_supervisor_pidfile() {
  printf '%s/service-supervisor.pid' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_service_log_file() {
  printf '%s/service.log' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
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
  mkdir -p "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
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
    "${UNIFY_GATEWAY_HOST:-127.0.0.1}" \
    "${UNIFY_GATEWAY_PORT:-8001}"
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

# --- Comms ingress bridge (internal-dev hosted Coordinator comms) ------------
# Polls hosted comms (Gmail for email, Twilio for SMS/WhatsApp) and forwards
# inbound items to the CM's local ingress, so the local Coordinator works
# without a public webhook. Each channel is active only when its credentials
# are configured; otherwise the bridge is a no-op (default fully-local stack).

self_host_comms_sa_file() {
  printf '%s' \
    "${SELF_HOST_COMMS_SA_FILE:-${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}/comms_sa.json}"
}

self_host_comms_bridge_pidfile() {
  printf '%s/comms-bridge.pid' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_comms_bridge_log_file() {
  printf '%s/comms-bridge.log' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_comms_bridge_script() {
  printf '%s/comms_ingress_bridge.py' "$_SELF_HOST_PACKAGED_DIR"
}

# Configured = at least one channel is set: a Gmail SA + Coordinator mailbox
# (email), or Twilio creds (SMS/WhatsApp). No-op otherwise.
self_host_comms_bridge_configured() {
  [[ -f "$(self_host_comms_sa_file)" && -n "${UNIFY_COORDINATOR_EMAIL_ADDRESS:-}" ]] && return 0
  [[ -n "${TWILIO_ACCOUNT_SID:-}" && -n "${TWILIO_AUTH_TOKEN:-}" ]] && return 0
  return 1
}

self_host_comms_bridge_is_running() {
  local pidfile pid
  pidfile="$(self_host_comms_bridge_pidfile)"
  [[ -f "$pidfile" ]] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

self_host_ensure_comms_bridge() {
  self_host_comms_bridge_configured || return 0
  self_host_comms_bridge_is_running && return 0

  local script py log_file
  script="$(self_host_comms_bridge_script)"
  [[ -f "$script" ]] || return 0
  py="${UNIFY_REPO_PATH:-}/.venv/bin/python"
  [[ -x "$py" ]] || py="python3"
  log_file="$(self_host_comms_bridge_log_file)"
  self_host_ensure_state_dir

  # Email channel keys off the comms SA + Coordinator mailbox; Twilio channels
  # key off TWILIO_* + the Coordinator numbers. ORCHESTRA_URL/ORCHESTRA_ADMIN_KEY
  # keep local routing identical to hosted adapters: every inbound sender is
  # resolved by Orchestra before the bridge forwards it to the local CM.
  GMAIL_BRIDGE_MAILBOX="${UNIFY_COORDINATOR_EMAIL_ADDRESS:-}" \
    GMAIL_BRIDGE_SA_FILE="$(self_host_comms_sa_file)" \
    COMMS_BRIDGE_SMS_NUMBER="${COMMS_BRIDGE_SMS_NUMBER:-${UNITY_COORDINATOR_PHONE:-}}" \
    COMMS_BRIDGE_WHATSAPP_NUMBER="${COMMS_BRIDGE_WHATSAPP_NUMBER:-${UNIFY_COORDINATOR_WHATSAPP_NUMBER:-}}" \
    ORCHESTRA_URL="${ORCHESTRA_URL:-http://127.0.0.1:8000/v0}" \
    ORCHESTRA_ADMIN_KEY="${ORCHESTRA_ADMIN_KEY:-}" \
    nohup "$py" "$script" >>"$log_file" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" >"$(self_host_comms_bridge_pidfile)"
}

self_host_stop_comms_bridge() {
  local pidfile pid
  pidfile="$(self_host_comms_bridge_pidfile)"
  [[ -f "$pidfile" ]] || return 0
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
}

# --- Inbound-call tunnel (cloudflared) ---------------------------------------
# Phone/WhatsApp calls need a live public webhook (a call is synchronous: Twilio
# POSTs the number's voice URL and needs TwiML back in seconds), unlike text,
# which the comms bridge polls. cloudflared exposes the local CM ingress
# (127.0.0.1:<port>/local/twilio/*) at a public https URL that the localhost
# number's voice webhook points at. Calls are part of the local product path by
# default; SELF_HOST_CALLS_ENABLED=0 is reserved for emergency local debugging.

self_host_local_comms_port() {
  printf '%s' "${UNIFY_CONVERSATION_LOCAL_COMMS_PORT:-8787}"
}

self_host_tunnel_pidfile() {
  printf '%s/call-tunnel.pid' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_tunnel_log_file() {
  printf '%s/call-tunnel.log' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_tunnel_url_file() {
  printf '%s/call-tunnel-url' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_sync_comms_script() {
  printf '%s/sync_comms_webhooks.py' "$_SELF_HOST_PACKAGED_DIR"
}

self_host_voice_synced_url_file() {
  printf '%s/call-voice-synced-url' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_tunnel_is_running() {
  local pidfile pid
  pidfile="$(self_host_tunnel_pidfile)"
  [[ -f "$pidfile" ]] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

self_host_tunnel_url() {
  local url_file
  url_file="$(self_host_tunnel_url_file)"
  [[ -f "$url_file" ]] || return 1
  cat "$url_file" 2>/dev/null || true
}

# Start the cloudflared quick tunnel and resolve its public URL. Idempotent: when
# already running with a recorded URL it just re-exports it. Returns non-zero
# (and leaves no URL) when cloudflared is missing or the URL can't be resolved.
self_host_ensure_tunnel() {
  self_host_calls_enabled || return 0
  self_host_ensure_state_dir

  if self_host_tunnel_is_running; then
    local existing
    existing="$(self_host_tunnel_url 2>/dev/null || true)"
    if [[ -n "$existing" ]]; then
      export UNIFY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL="$existing"
      return 0
    fi
    # Running but URL not yet recorded — fall through to re-resolve from the log.
  fi

  command -v cloudflared >/dev/null 2>&1 || {
    echo "[call-tunnel] cloudflared not installed — inbound calls unavailable" >&2
    return 1
  }

  local port log_file url_file
  port="$(self_host_local_comms_port)"
  log_file="$(self_host_tunnel_log_file)"
  url_file="$(self_host_tunnel_url_file)"
  rm -f "$url_file"

  if ! self_host_tunnel_is_running; then
    : >"$log_file"
    nohup cloudflared tunnel --no-autoupdate \
      --url "http://127.0.0.1:${port}" >>"$log_file" 2>&1 &
    local pid=$!
    disown "$pid" 2>/dev/null || true
    echo "$pid" >"$(self_host_tunnel_pidfile)"
  fi

  # cloudflared prints the assigned https://<sub>.trycloudflare.com to its log.
  local url="" attempt
  for attempt in $(seq 1 40); do
    url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$log_file" 2>/dev/null | head -1 || true)"
    [[ -n "$url" ]] && break
    sleep 0.5
  done

  if [[ -z "$url" ]]; then
    echo "[call-tunnel] failed to resolve tunnel URL — see $log_file" >&2
    return 1
  fi

  printf '%s' "$url" >"$url_file"
  export UNIFY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL="$url"
  # The tunnel exposes the CM comms ingress (8787), NOT the gateway (8001).
  # unity/scripts/local.sh defaults UNIFY_GATEWAY_PUBLIC_URL to the comms public
  # URL when unset, which would point the gateway health check + outbound
  # callback base at the comms tunnel. Pin the gateway to its local URL so only
  # the CM ingress is tunneled.
  export UNIFY_GATEWAY_PUBLIC_URL="${UNIFY_GATEWAY_PUBLIC_URL:-http://127.0.0.1:${UNIFY_GATEWAY_PORT:-8001}}"
  if declare -F self_host_patch_runtime_state &>/dev/null; then
    self_host_patch_runtime_state "call_tunnel_url=$url" || true
  fi
  return 0
}

self_host_stop_tunnel() {
  local pidfile pid
  pidfile="$(self_host_tunnel_pidfile)"
  if [[ -f "$pidfile" ]]; then
    pid="$(cat "$pidfile" 2>/dev/null || true)"
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
    rm -f "$pidfile"
  fi
  rm -f "$(self_host_tunnel_url_file)" "$(self_host_voice_synced_url_file)"
  if declare -F self_host_patch_runtime_state &>/dev/null; then
    self_host_patch_runtime_state "call_tunnel_url=" || true
  fi
}

# --- Provider-event trigger worker -------------------------------------------
# Reconciliation and dispatch run in a dedicated Orchestra worker process.
# Unlike phone calls, Unify does not provide a managed callback tunnel: operators
# point their own HTTPS reverse proxy at Orchestra's webhook route.

self_host_provider_trigger_worker_port() {
  printf '%s' "${SELF_HOST_PROVIDER_TRIGGER_WORKER_PORT:-8081}"
}

self_host_provider_trigger_worker_pidfile() {
  printf '%s/provider-trigger-worker.pid' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_provider_trigger_worker_log_file() {
  printf '%s/provider-trigger-worker.log' "${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}"
}

self_host_provider_trigger_worker_is_running() {
  local pidfile pid
  pidfile="$(self_host_provider_trigger_worker_pidfile)"
  [[ -f "$pidfile" ]] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

self_host_provider_trigger_worker_is_healthy() {
  self_host_provider_trigger_worker_is_running || return 1
  local port
  port="$(self_host_provider_trigger_worker_port)"
  curl -fsS --max-time 5 "http://127.0.0.1:${port}/ready" >/dev/null 2>&1
}

self_host_ensure_provider_trigger_worker() {
  if ! declare -F self_host_provider_triggers_enabled &>/dev/null \
    || ! self_host_provider_triggers_enabled; then
    return 0
  fi
  if ! declare -F self_host_validate_provider_trigger_config &>/dev/null \
    || ! self_host_validate_provider_trigger_config; then
    return 1
  fi
  if declare -F self_host_export_provider_trigger_env &>/dev/null; then
    self_host_export_provider_trigger_env
  fi
  if self_host_provider_trigger_worker_is_healthy; then
    return 0
  fi
  if self_host_provider_trigger_worker_is_running; then
    self_host_stop_provider_trigger_worker
  fi

  local orchestra_repo py log_file port db_port gateway_port
  orchestra_repo="${ORCHESTRA_REPO_PATH:-}"
  [[ -n "$orchestra_repo" && -d "$orchestra_repo" ]] || {
    echo "[provider-trigger-worker] ORCHESTRA_REPO_PATH is not set" >&2
    return 1
  }
  py="${orchestra_repo}/.venv/bin/python"
  [[ -x "$py" ]] || py="python3"
  log_file="$(self_host_provider_trigger_worker_log_file)"
  port="$(self_host_provider_trigger_worker_port)"
  db_port="${ORCHESTRA_DB_PORT:-55432}"
  gateway_port="${UNIFY_GATEWAY_PORT:-8001}"
  self_host_ensure_state_dir

  PORT="$port" \
    ORCHESTRA_DB_HOST=localhost \
    ORCHESTRA_DB_PORT="$db_port" \
    ORCHESTRA_DB_USER=orchestra \
    ORCHESTRA_DB_PASS=orchestra \
    ORCHESTRA_DB_BASE=orchestra \
    SELF_HOST=1 \
    UNIFY_COMMS_URL="${UNIFY_COMMS_URL:-http://127.0.0.1:${gateway_port}}" \
    UNIFY_ADAPTERS_URL="${UNIFY_ADAPTERS_URL:-http://127.0.0.1:${gateway_port}}" \
    nohup "$py" -m orchestra.workers.provider_trigger_worker >>"$log_file" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" >"$(self_host_provider_trigger_worker_pidfile)"

  local attempt
  for attempt in $(seq 1 30); do
    if self_host_provider_trigger_worker_is_healthy; then
      return 0
    fi
    sleep 1
  done
  echo "[provider-trigger-worker] worker did not become ready — see $log_file" >&2
  return 1
}

self_host_stop_provider_trigger_worker() {
  local pidfile pid
  pidfile="$(self_host_provider_trigger_worker_pidfile)"
  [[ -f "$pidfile" ]] || return 0
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
}

# Re-point the localhost number's Twilio voice webhook at the current tunnel when
# the tunnel URL changed (e.g. cloudflared restarted with a fresh quick-tunnel
# host). Called from the runtime supervisor loop. No-op only when explicitly
# disabled, no tunnel URL is known, or the URL is unchanged since last sync.
self_host_resync_voice_webhooks_if_changed() {
  self_host_calls_enabled || return 0
  local url marker prev script py
  url="$(self_host_tunnel_url 2>/dev/null || true)"
  [[ -n "$url" ]] || return 0
  marker="$(self_host_voice_synced_url_file)"
  prev=""
  [[ -f "$marker" ]] && prev="$(cat "$marker" 2>/dev/null || true)"
  [[ "$url" == "$prev" ]] && return 0
  script="$(self_host_sync_comms_script)"
  [[ -f "$script" ]] || return 0
  py="${UNIFY_REPO_PATH:-}/.venv/bin/python"
  [[ -x "$py" ]] || py="python3"
  if UNIFY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL="$url" \
    "$py" "$script" --set-voice >/dev/null 2>&1; then
    printf '%s' "$url" >"$marker"
  fi
}

# Restore the voice callback state this installation replaced. A changed
# callback is never overwritten during release.
self_host_revert_voice_webhooks() {
  self_host_calls_enabled || return 0
  local script py
  script="$(self_host_sync_comms_script)"
  [[ -f "$script" ]] || return 0
  if declare -F self_host_export_comms_twilio &>/dev/null; then
    self_host_export_comms_twilio
  fi
  py="${UNIFY_REPO_PATH:-}/.venv/bin/python"
  [[ -x "$py" ]] || py="python3"
  if ! "$py" "$script" --release-voice >/dev/null 2>&1; then
    echo "[call-tunnel] owned voice callback release failed" >&2
    return 1
  fi
  rm -f "$(self_host_voice_synced_url_file)"
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
  main_pids="$(pgrep -f "[Pp]ython.*-m unity\.conversation_manager\.main" 2>/dev/null || true)"
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
  return 0
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
  pid="$(pgrep -f "[b]ash .*/service\.sh run" 2>/dev/null | head -1 || true)"
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
  export UNIFY_RUNTIME_OWNER="$SELF_HOST_RUNTIME_OWNER_SERVICE"
  export UNIFY_SERVICE_RUNTIME=1
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
  if self_host_gateway_is_healthy; then
    printf 'gateway: running (%s)\n' "$(self_host_gateway_base_url)"
  else
    printf 'gateway: stopped (%s)\n' "$(self_host_gateway_base_url)"
  fi
  if self_host_comms_bridge_configured; then
    local _bridge_channels=""
    [[ -n "${UNIFY_COORDINATOR_EMAIL_ADDRESS:-}" && -f "$(self_host_comms_sa_file)" ]] \
      && _bridge_channels="email"
    if [[ -n "${TWILIO_ACCOUNT_SID:-}" && -n "${TWILIO_AUTH_TOKEN:-}" \
      && -n "${ORCHESTRA_ADMIN_KEY:-}" \
      && ( -n "${COMMS_BRIDGE_SMS_NUMBER:-}" || -n "${COMMS_BRIDGE_WHATSAPP_NUMBER:-}" ) ]]; then
      _bridge_channels="${_bridge_channels:+$_bridge_channels,}sms,whatsapp"
    fi
    if self_host_comms_bridge_is_running; then
      printf 'comms-bridge: running (%s)\n' "${_bridge_channels:-configured}"
    else
      printf 'comms-bridge: stopped\n'
    fi
  fi
  if self_host_tunnel_is_running; then
    printf 'call-tunnel: running (%s)\n' "$(self_host_tunnel_url 2>/dev/null || echo '?')"
  else
    printf 'call-tunnel: stopped\n'
  fi
  if declare -F self_host_provider_triggers_enabled &>/dev/null \
    && self_host_provider_triggers_enabled; then
    if self_host_provider_trigger_worker_is_healthy; then
      printf 'provider-trigger-worker: healthy (port %s)\n' "$(self_host_provider_trigger_worker_port)"
    elif self_host_provider_trigger_worker_is_running; then
      printf 'provider-trigger-worker: running (not ready, port %s)\n' "$(self_host_provider_trigger_worker_port)"
    else
      printf 'provider-trigger-worker: stopped\n'
    fi
    if [[ -n "${ORCHESTRA_TRIGGER_CALLBACK_BASE_URL:-}" ]]; then
      printf 'provider-callback: %s\n' "$ORCHESTRA_TRIGGER_CALLBACK_BASE_URL"
    fi
  fi
}

self_host_load_coordinator_credentials() {
  local runtime_file="${1:-${SELF_HOST_COORDINATOR_RUNTIME_FILE:-}}"
  if [[ -z "$runtime_file" ]]; then
    if declare -F self_host_coordinator_runtime_file &>/dev/null; then
      runtime_file="$(self_host_coordinator_runtime_file)"
    else
      runtime_file="${SELF_HOST_STATE_DIR:-${UNIFY_HOME:-$HOME/.unity}}/coordinator-runtime.json"
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
