#!/usr/bin/env bash
# =============================================================================
# self_host_env.sh — Load self-host runtime env from unity/.env
# =============================================================================
#
# Sources BYOK keys from unity/.env into the runtime environment for Orchestra,
# the gateway, and the Coordinator CM.
#
set -euo pipefail

# Matches get_local_root() in unity/file_manager/settings.py (~/Unity/Local).
SELF_HOST_DEFAULT_WORKSPACE="${SELF_HOST_DEFAULT_WORKSPACE:-$HOME/Unity/Local}"
SELF_HOST_COORDINATOR_VOICE_PROVIDER="${SELF_HOST_COORDINATOR_VOICE_PROVIDER:-elevenlabs}"
SELF_HOST_COORDINATOR_VOICE_ID="${SELF_HOST_COORDINATOR_VOICE_ID:-iP95p4xoKVk53GoZ742B}"
# Shared Coordinator contact identities, dedicated per deployment mode so that
# staging/production traffic never collides with localhost. Each mode owns a
# distinct WhatsApp number, SMS/voice number, and email mailbox:
#
#   mode       console URL                                              WhatsApp
#   ---------  ------------------------------------------------------   --------------
#   production https://console.unify.ai/                                +447700900012
#   staging    https://internal.example.com +447700900013
#   localhost  http://localhost:3000/                                   +447700900001
#
# The localhost identities below are the live defaults used by the self-host
# stack. They are "poll-only": their Twilio inbound webhooks are cleared so a
# hosted backend never answers localhost traffic — the comms ingress bridge
# polls Twilio and forwards inbound to the local CM. Run
# `selfhost/sync_comms_webhooks.py` (or `stack.sh sync-comms`) to enforce this.
SELF_HOST_COORDINATOR_EMAIL_ADDRESS="${SELF_HOST_COORDINATOR_EMAIL_ADDRESS:-local-twin@unify.ai}"
SELF_HOST_COORDINATOR_PHONE_US="${SELF_HOST_COORDINATOR_PHONE_US:-+15550100010}"
SELF_HOST_COORDINATOR_WHATSAPP_NUMBER="${SELF_HOST_COORDINATOR_WHATSAPP_NUMBER:-+447700900001}"
SELF_HOST_COORDINATOR_DEFAULT_PHONE_COUNTRY="${SELF_HOST_COORDINATOR_DEFAULT_PHONE_COUNTRY:-US}"

# Inbound/outbound phone & WhatsApp calls are part of the default self-host
# stack. Unlike text (which the comms ingress bridge polls), a call is
# synchronous: Twilio POSTs the number's voice webhook and needs TwiML back in
# seconds, so calls need a live public webhook (a cloudflared tunnel to the local
# CM ingress) and a LiveKit Cloud SIP trunk for the media leg. Set
# SELF_HOST_CALLS_ENABLED=0 only for an explicitly poll-only text stack.
SELF_HOST_CALLS_ENABLED="${SELF_HOST_CALLS_ENABLED:-1}"

# The self-host compose bundle (entrypoints, fetch helpers) lives alongside this
# script in unity-deploy/deploy/selfhost/.
_SELF_HOST_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SELF_HOST_DEPLOY_SELFHOST_DIR="${SELF_HOST_DEPLOY_SELFHOST_DIR:-$_SELF_HOST_ENV_DIR/../deploy/selfhost}"

# Persistent self-host state (survives reboot; unlike /tmp).
SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}"

self_host_coordinator_runtime_file() {
  printf '%s/coordinator-runtime.json' "$SELF_HOST_STATE_DIR"
}

export_self_host_coordinator_runtime_file() {
  export SELF_HOST_COORDINATOR_RUNTIME_FILE="$(self_host_coordinator_runtime_file)"
  mkdir -p "$SELF_HOST_STATE_DIR"
}

default_self_host_workspace() {
  printf '%s' "${UNITY_LOCAL_ROOT:-$SELF_HOST_DEFAULT_WORKSPACE}"
}

ensure_self_host_workspace_dir() {
  local workspace
  workspace="$(default_self_host_workspace)"
  mkdir -p "$workspace"
}

self_host_export_coordinator_contact_env() {
  # The local Coordinator uses fixed, shared contact identities so every
  # developer's localhost deployment converges on the same Twin contact rows.
  export UNITY_COORDINATOR_EMAIL_ADDRESS="$SELF_HOST_COORDINATOR_EMAIL_ADDRESS"
  export ORCHESTRA_UNITY_COORDINATOR_EMAIL_ADDRESS="${ORCHESTRA_UNITY_COORDINATOR_EMAIL_ADDRESS:-$UNITY_COORDINATOR_EMAIL_ADDRESS}"

  export UNITY_COORDINATOR_PHONE_US="$SELF_HOST_COORDINATOR_PHONE_US"
  export ORCHESTRA_UNITY_COORDINATOR_PHONE_US="${ORCHESTRA_UNITY_COORDINATOR_PHONE_US:-$UNITY_COORDINATOR_PHONE_US}"
  export UNITY_COORDINATOR_DEFAULT_PHONE_COUNTRY="$SELF_HOST_COORDINATOR_DEFAULT_PHONE_COUNTRY"
  export ORCHESTRA_UNITY_COORDINATOR_DEFAULT_PHONE_COUNTRY="${ORCHESTRA_UNITY_COORDINATOR_DEFAULT_PHONE_COUNTRY:-$UNITY_COORDINATOR_DEFAULT_PHONE_COUNTRY}"

  export UNITY_COORDINATOR_PHONE="${SELF_HOST_COORDINATOR_PHONE:-$UNITY_COORDINATOR_PHONE_US}"
  export ASSISTANT_NUMBER="$UNITY_COORDINATOR_PHONE"
  export COMMS_BRIDGE_SMS_NUMBER="$UNITY_COORDINATOR_PHONE"

  export UNITY_COORDINATOR_WHATSAPP_NUMBER="$SELF_HOST_COORDINATOR_WHATSAPP_NUMBER"
  export ASSISTANT_WHATSAPP_NUMBER="$UNITY_COORDINATOR_WHATSAPP_NUMBER"
  export COMMS_BRIDGE_WHATSAPP_NUMBER="$UNITY_COORDINATOR_WHATSAPP_NUMBER"
}

self_host_export_coordinator_contact_env

load_self_host_env_file() {
  local env_file="${1:-}"
  if [[ -z "$env_file" || ! -f "$env_file" ]]; then
    self_host_export_coordinator_contact_env
    return 0
  fi
  # Parse KEY=VALUE lines only — never `source` the whole file, which breaks on
  # orphan values or duplicate keys that produce multiline upserts.
  local exports
  exports="$(python3 - "$env_file" <<'PYEOF'
import os
import re
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
skip = {
    "UNIFY_KEY",
    "SHARED_UNIFY_KEY",
    "unify_key",
    "ORCHESTRA_URL",
    "UNITY_COMMS_URL",
    "UNITY_ADAPTERS_URL",
    # Self-host always runs with Console, so the stack forces UNITY_CONSOLE_UI=on
    # (see stack.sh / service.sh). Ignore the public unity/.env value, which is
    # the headless-install default, so it can't clobber the stack's export.
    "UNITY_CONSOLE_UI",
}
if os.environ.get("SELF_HOST_SKIP_LIVEKIT_ENV_KEYS", "") == "1":
    skip.update(
        {
            "LIVEKIT_URL",
            "LIVEKIT_API_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
            "LIVEKIT_SIP_URI",
        },
    )
key_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
seen: set[str] = set()
for raw in path.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        continue
    key, _, val = line.partition("=")
    key = key.strip()
    if key in skip or not key_re.match(key) or key in seen:
        continue
    seen.add(key)
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1]
    print(f"export {shlex.quote(key)}={shlex.quote(val)}")
PYEOF
)"
  if [[ -n "$exports" ]]; then
    # shellcheck disable=SC1090
    eval "$exports"
  fi
  self_host_export_coordinator_contact_env
}

load_self_host_repo_env_file() {
  SELF_HOST_SKIP_LIVEKIT_ENV_KEYS=1 load_self_host_env_file "$@"
}

self_host_export_comms_sa() {
  # Internal-dev hosted Coordinator email: export the comms service-account key
  # so the gateway can send Coordinator email through the hosted Gmail mailbox
  # (and the comms ingress bridge can poll replies). The key is read only from
  # the self-host state dir (~/.unity by default) and never written to a repo.
  # No-op when absent, so the default fully-local stack is unchanged.
  local sa_file
  sa_file="${SELF_HOST_COMMS_SA_FILE:-${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}/comms_sa.json}"
  if [[ ! -f "$sa_file" ]]; then
    return 0
  fi
  local sa_json
  sa_json="$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1]))))' "$sa_file" 2>/dev/null || true)"
  if [[ -n "$sa_json" ]]; then
    export GCP_SA_KEY="$sa_json"
  fi
}

self_host_export_comms_twilio() {
  # Internal-dev hosted Coordinator SMS/WhatsApp: load Twilio creds + the
  # Coordinator numbers so the gateway can send as the Coordinator and the comms
  # bridge can poll inbound. Read only from the self-host state dir; never
  # written to a repo. No-op when absent, so the default stack is unchanged.
  local twilio_file
  twilio_file="${SELF_HOST_COMMS_TWILIO_FILE:-${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}/comms_twilio.env}"
  [[ -f "$twilio_file" ]] || return 0
  load_self_host_env_file "$twilio_file"
  # Twilio credentials live in the local state file; Coordinator numbers come
  # from the canonical self-host contact env above.
  self_host_export_coordinator_contact_env
}

self_host_calls_enabled() {
  # True when phone/WhatsApp call support is turned on.
  case "${SELF_HOST_CALLS_ENABLED:-0}" in
    1 | true | TRUE | yes | YES | on | ON) return 0 ;;
    *) return 1 ;;
  esac
}

self_host_livekit_cloud_file() {
  printf '%s' \
    "${SELF_HOST_LIVEKIT_CLOUD_FILE:-${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}/livekit_cloud.env}"
}

self_host_export_livekit_cloud() {
  # The source stack uses LiveKit Cloud for browser media and SIP. Credentials
  # live in the self-host state dir, never a repo .env file.
  local lk_file
  lk_file="$(self_host_livekit_cloud_file)"
  [[ -f "$lk_file" ]] || return 0
  load_self_host_env_file "$lk_file"
}

self_host_export_livekit_backend() {
  self_host_export_livekit_cloud
}

self_host_livekit_cloud_url_configured() {
  case "${LIVEKIT_URL:-}" in
    "" | ws://localhost* | ws://127.* | http://localhost* | http://127.*) return 1 ;;
    *) return 0 ;;
  esac
}

self_host_livekit_media_configured() {
  self_host_livekit_cloud_url_configured \
    && [[ -n "${LIVEKIT_API_KEY:-}" && -n "${LIVEKIT_API_SECRET:-}" ]]
}

self_host_livekit_sip_configured() {
  self_host_livekit_media_configured && [[ -n "${LIVEKIT_SIP_URI:-}" ]]
}

self_host_apply_user_desktops_export() {
  local agent_id="${1:-}"
  local script="${SELF_HOST_DEPLOY_SELFHOST_DIR}/fetch_assistant_user_desktops.py"
  local admin_key="${ORCHESTRA_ADMIN_KEY:-}"
  local orchestra_url="${ORCHESTRA_URL:-http://127.0.0.1:8000/v0}"
  local export_line=""

  if [[ -z "$agent_id" || ! -f "$script" ]]; then
    return 0
  fi

  if [[ -z "$admin_key" && -n "${ENV_LOCAL:-}" && -f "$ENV_LOCAL" ]]; then
    admin_key="$(grep -E "^ORCHESTRA_ADMIN_KEY=" "$ENV_LOCAL" 2>/dev/null | sed 's/^[^=]*=//' | tr -d '"' || true)"
  fi
  if [[ -z "$admin_key" ]]; then
    return 0
  fi

  export_line="$(
    ORCHESTRA_URL="${orchestra_url%/}" \
      ORCHESTRA_ADMIN_KEY="$admin_key" \
      python3 "$script" "$agent_id" --export-sh 2>/dev/null || true
  )"
  if [[ -n "$export_line" ]]; then
    # shellcheck disable=SC1090
    eval "$export_line"
  fi
}

append_self_host_unity_runtime_env() {
  local -n _target_array="$1"
  local env_file="${2:-${SELF_HOST_ENV_FILE:-${UNITY_ENV_FILE:-${UNITY_REPO:-}/.env}}}"
  load_self_host_repo_env_file "$env_file"

  local workspace
  workspace="$(default_self_host_workspace)"
  ensure_self_host_workspace_dir
  _target_array+=("UNITY_LOCAL_ROOT=$workspace")
  _target_array+=(
    "UNITY_CONVERSATION_LOCAL_COMMS_ENABLED=${UNITY_CONVERSATION_LOCAL_COMMS_ENABLED:-true}"
    "UNITY_CONVERSATION_LOCAL_COMMS_MODE=${UNITY_CONVERSATION_LOCAL_COMMS_MODE:-local}"
    "UNITY_CONVERSATION_LOCAL_COMMS_HOST=${UNITY_CONVERSATION_LOCAL_COMMS_HOST:-127.0.0.1}"
    "UNITY_CONVERSATION_LOCAL_COMMS_PORT=${UNITY_CONVERSATION_LOCAL_COMMS_PORT:-8787}"
    "UNITY_GATEWAY_LOG_LEVEL=${UNITY_GATEWAY_LOG_LEVEL:-debug}"
    "PYTHONFAULTHANDLER=${PYTHONFAULTHANDLER:-1}"
  )

  # Local deployment is a debugging surface like the test suite (rapid iteration,
  # breaking changes), so it adopts the test harness logging convention
  # (unity/tests/parallel_run.sh): cross-repo OTel spans from unity, unify, and
  # unillm aggregate into the unity repo's logs/all/ (one {trace_id}.jsonl per
  # run), while each repo's full file logs — including the LLM request/response
  # with reasoning — land in a per-repo logs/<repo>/ dir. All are opt-out: any
  # value exported beforehand wins. Orchestra runs as a separate process; its
  # equivalent dirs are set where it is launched (console start_orchestra).
  local _unity_repo_root="${UNITY_REPO_PATH:-${UNITY_REPO:-}}"
  if [[ -n "$_unity_repo_root" ]]; then
    local _otel_log_dir="${UNITY_OTEL_LOG_DIR:-$_unity_repo_root/logs/all}"
    mkdir -p \
      "$_otel_log_dir" \
      "$_unity_repo_root/logs/unity" \
      "$_unity_repo_root/logs/unify" \
      "$_unity_repo_root/logs/unillm" 2>/dev/null || true
    _target_array+=(
      "UNITY_OTEL=${UNITY_OTEL:-true}"
      "UNIFY_OTEL=${UNIFY_OTEL:-true}"
      "UNILLM_OTEL=${UNILLM_OTEL:-true}"
      "UNITY_OTEL_LOG_DIR=$_otel_log_dir"
      "UNIFY_OTEL_LOG_DIR=${UNIFY_OTEL_LOG_DIR:-$_otel_log_dir}"
      "UNILLM_OTEL_LOG_DIR=${UNILLM_OTEL_LOG_DIR:-$_otel_log_dir}"
      "UNITY_LOG_DIR=${UNITY_LOG_DIR:-$_unity_repo_root/logs/unity}"
      "UNIFY_LOG_DIR=${UNIFY_LOG_DIR:-$_unity_repo_root/logs/unify}"
      "UNILLM_LOG_DIR=${UNILLM_LOG_DIR:-$_unity_repo_root/logs/unillm}"
    )
  fi

  if [[ "${VOICE_PROVIDER:-}" == "elevenlabs" && -z "${VOICE_ID:-}" ]]; then
    _target_array+=("VOICE_ID=$SELF_HOST_COORDINATOR_VOICE_ID")
  fi

  self_host_export_livekit_backend
  local _livekit_key
  for _livekit_key in \
    LIVEKIT_URL \
    LIVEKIT_API_URL \
    LIVEKIT_API_KEY \
    LIVEKIT_API_SECRET \
    LIVEKIT_SIP_URI \
    LIVEKIT_EGRESS_GCS_BUCKET; do
    if [[ -n "${!_livekit_key:-}" ]]; then
      _target_array+=("$_livekit_key=${!_livekit_key}")
    fi
  done
  if [[ -n "${UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL:-}" ]]; then
    _target_array+=(
      "UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL=${UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL}"
    )
  fi

  local key val
  for key in \
    UNITY_WEB_TAVILY_API_KEY \
    UNITY_WEB_ENABLED \
    UNITY_ACTOR_ANTICAPTCHA_KEY \
    ANTICAPTCHA_KEY \
    UNIFY_MODEL \
    OPENAI_API_KEY \
    ANTHROPIC_API_KEY \
    DEEPSEEK_API_KEY \
    DEEPGRAM_API_KEY \
    CARTESIA_API_KEY \
    ELEVEN_API_KEY \
    VOICE_PROVIDER \
    VOICE_ID; do
    val="${!key:-}"
    if [[ -n "$val" ]]; then
      _target_array+=("$key=$val")
    fi
  done
}

if [[ -z "${SELF_HOST_RUNTIME_HELPERS_LOADED:-}" ]]; then
  _self_host_runtime_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  # shellcheck source=scripts/self_host_runtime.sh
  source "$_self_host_runtime_dir/self_host_runtime.sh"
  SELF_HOST_RUNTIME_HELPERS_LOADED=1
fi
