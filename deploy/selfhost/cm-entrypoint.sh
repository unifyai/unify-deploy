#!/usr/bin/env bash
# Self-host CM supervisor: watch coordinator-runtime.json and run ConversationManager.
set -euo pipefail

RUNTIME_FILE="${SELF_HOST_COORDINATOR_RUNTIME_FILE:-/runtime/coordinator-runtime.json}"
POLL_SECONDS="${SELF_HOST_CM_POLL_SECONDS:-2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[unity-cm] $*"; }

wait_for_runtime() {
  while [[ ! -f "$RUNTIME_FILE" ]]; do
    log "Waiting for coordinator runtime file at ${RUNTIME_FILE}..."
    sleep "$POLL_SECONDS"
  done
}

read_runtime() {
  python3 - "$RUNTIME_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
print(data.get("apiKey") or data.get("api_key") or "")
print(data.get("coordinatorAgentId") or data.get("coordinator_agent_id") or "")
PY
}

fetch_assistant_field() {
  local unify_key="$1"
  local agent_id="$2"
  local field="$3"
  python3 "${SCRIPT_DIR}/fetch_assistant_field.py" "$unify_key" "$agent_id" "$field" 2>/dev/null || true
}

build_cm_env() {
  local unify_key="$1"
  local agent_id="$2"

  export ASSISTANT_ID="$agent_id"
  export UNIFY_KEY="$unify_key"
  export SHARED_UNIFY_KEY="$unify_key"
  export SELF_HOST=1
  export DEPLOY_ENV="${DEPLOY_ENV:-staging}"
  export ASSISTANT_IS_COORDINATOR=True
  export EVENTBUS_PUBLISHING_ENABLED="${EVENTBUS_PUBLISHING_ENABLED:-true}"
  export EVENTBUS_PUBSUB_STREAMING="${EVENTBUS_PUBSUB_STREAMING:-true}"
  export UNITY_LOCAL_SCHEDULER="${UNITY_LOCAL_SCHEDULER:-true}"
  export GRPC_VERBOSITY="${GRPC_VERBOSITY:-ERROR}"
  export UNITY_RUNTIME_OWNER="${UNITY_RUNTIME_OWNER:-compose}"
  export UNITY_DESKTOP_SHARED_MOUNT="${UNITY_DESKTOP_SHARED_MOUNT:-1}"
  export UNITY_LOCAL_ROOT="${UNITY_LOCAL_ROOT:-/Unity/Local}"

  export PUBSUB_EMULATOR_HOST="${PUBSUB_EMULATOR_HOST:-pubsub-emulator:8085}"
  export GCP_PROJECT_ID="${GCP_PROJECT_ID:-local-test-project}"
  export ORCHESTRA_URL="${ORCHESTRA_URL:-http://orchestra:8000/v0}"
  export UNITY_COMMS_URL="${UNITY_COMMS_URL:-http://gateway:8001}"
  export UNITY_ADAPTERS_URL="${UNITY_ADAPTERS_URL:-http://gateway:8001}"

  export UNITY_CONVERSATION_LOCAL_COMMS_ENABLED=true
  export UNITY_CONVERSATION_LOCAL_COMMS_MODE=local
  export UNITY_CONVERSATION_LOCAL_COMMS_HOST="${UNITY_CONVERSATION_LOCAL_COMMS_HOST:-0.0.0.0}"
  export UNITY_CONVERSATION_LOCAL_COMMS_PORT="${UNITY_CONVERSATION_LOCAL_COMMS_PORT:-8787}"

  export LIVEKIT_URL="${LIVEKIT_URL:-ws://livekit:7880}"
  export LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-devkey}"
  export LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-secret}"

  # Voice jobs need the turn-detector assets baked at image build time (/opt/hf-cache).
  # Production entrypoint.sh seeds these into /tmp/huggingface; self-host CM bypasses that path.
  if [[ -z "${HF_HOME:-}" ]]; then
    if [[ -d /opt/hf-cache ]]; then
      export HF_HOME=/opt/hf-cache
    elif [[ -d /tmp/huggingface ]]; then
      export HF_HOME=/tmp/huggingface
    fi
  fi

  if [[ -n "${SELF_HOST_DESKTOP_INTERNAL_URL:-}" ]]; then
    export ASSISTANT_DESKTOP_URL="${SELF_HOST_DESKTOP_INTERNAL_URL%/}"
  elif [[ -n "${SELF_HOST_DESKTOP_URL:-}" ]]; then
    export ASSISTANT_DESKTOP_URL="${SELF_HOST_DESKTOP_URL%/}"
  fi

  local voice_provider voice_id
  voice_provider="$(fetch_assistant_field "$unify_key" "$agent_id" "voice_provider" || true)"
  voice_id="$(fetch_assistant_field "$unify_key" "$agent_id" "voice_id" || true)"
  # A VOICE_PROVIDER/VOICE_ID provided via the environment (BYOK .env) is
  # authoritative for self-host; fall back to the assistant's stored values.
  [[ -z "${VOICE_PROVIDER:-}" && -n "$voice_provider" ]] && export VOICE_PROVIDER="$voice_provider"
  [[ -z "${VOICE_ID:-}" && -n "$voice_id" ]] && export VOICE_ID="$voice_id"

  local first_name surname about age nationality timezone user_id
  first_name="$(fetch_assistant_field "$unify_key" "$agent_id" "first_name" || true)"
  surname="$(fetch_assistant_field "$unify_key" "$agent_id" "surname" || true)"
  about="$(fetch_assistant_field "$unify_key" "$agent_id" "about" || true)"
  age="$(fetch_assistant_field "$unify_key" "$agent_id" "age" || true)"
  nationality="$(fetch_assistant_field "$unify_key" "$agent_id" "nationality" || true)"
  timezone="$(fetch_assistant_field "$unify_key" "$agent_id" "timezone" || true)"
  user_id="$(fetch_assistant_field "$unify_key" "$agent_id" "user_id" || true)"

  [[ -n "$first_name" ]] && export ASSISTANT_FIRST_NAME="$first_name"
  [[ -n "$surname" ]] && export ASSISTANT_SURNAME="$surname"
  [[ -n "$about" ]] && export ASSISTANT_ABOUT="$about"
  [[ -n "$age" ]] && export ASSISTANT_AGE="$age"
  [[ -n "$nationality" ]] && export ASSISTANT_NATIONALITY="$nationality"
  [[ -n "$timezone" ]] && export ASSISTANT_TIMEZONE="$timezone"
  [[ -n "$user_id" ]] && export USER_ID="$user_id"

  local user_desktops_export=""
  if [[ -n "${ORCHESTRA_ADMIN_KEY:-}" ]]; then
    user_desktops_export="$(
      ORCHESTRA_URL="${ORCHESTRA_URL%/}" \
        ORCHESTRA_ADMIN_KEY="${ORCHESTRA_ADMIN_KEY}" \
        python3 "${SCRIPT_DIR}/fetch_assistant_user_desktops.py" "$agent_id" --export-sh 2>/dev/null || true
    )"
  fi
  if [[ -n "$user_desktops_export" ]]; then
    eval "$user_desktops_export"
  fi
}

runtime_signature() {
  python3 - "$RUNTIME_FILE" <<'PY'
import hashlib
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
api_key = data.get("apiKey") or data.get("api_key") or ""
agent_id = data.get("coordinatorAgentId") or data.get("coordinator_agent_id") or ""
print(hashlib.sha256(f"{agent_id}\0{api_key}".encode()).hexdigest())
PY
}

start_cm() {
  local unify_key="$1"
  local agent_id="$2"

  log "Ensuring Pub/Sub topics for assistant ${agent_id}..."
  bash "${SCRIPT_DIR}/ensure-pubsub-topics.sh" "$agent_id"

  build_cm_env "$unify_key" "$agent_id"
  log "Starting ConversationManager for assistant ${agent_id}..."
  python3 -m unity.conversation_manager.main &
  CM_PID=$!
}

while true; do
  wait_for_runtime
  parsed="$(read_runtime)"
  unify_key="$(echo "$parsed" | sed -n '1p')"
  agent_id="$(echo "$parsed" | sed -n '2p')"

  if [[ -z "$unify_key" || -z "$agent_id" ]]; then
    log "Runtime file present but missing credentials; retrying..."
    sleep "$POLL_SECONDS"
    continue
  fi

  active_signature="$(runtime_signature)"
  start_cm "$unify_key" "$agent_id"

  while kill -0 "$CM_PID" 2>/dev/null; do
    sleep "$POLL_SECONDS"
    parsed="$(read_runtime)"
    next_unify_key="$(echo "$parsed" | sed -n '1p')"
    next_agent_id="$(echo "$parsed" | sed -n '2p')"
    if [[ -z "$next_unify_key" || -z "$next_agent_id" ]]; then
      continue
    fi
    next_signature="$(runtime_signature)"
    if [[ "$next_signature" != "$active_signature" ]]; then
      log "Coordinator runtime changed; restarting CM..."
      kill "$CM_PID" 2>/dev/null || true
      wait "$CM_PID" 2>/dev/null || true
      break
    fi
  done

  if kill -0 "$CM_PID" 2>/dev/null; then
    continue
  fi
  wait "$CM_PID" 2>/dev/null || true
  log "CM exited; watching for runtime updates..."
done
