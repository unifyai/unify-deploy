#!/usr/bin/env bash
# =============================================================================
# self_host_env.sh — Load self-host runtime env from droid/.env
# =============================================================================
#
# Sources BYOK keys from droid/.env into the runtime environment for Orchestra,
# the gateway, and the Coordinator CM.
#
set -euo pipefail

# Matches get_local_root() in droid/file_manager/settings.py (~/Droid/Local).
SELF_HOST_DEFAULT_WORKSPACE="${SELF_HOST_DEFAULT_WORKSPACE:-$HOME/Droid/Local}"
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

# Inbound/outbound phone & WhatsApp calls are opt-in. Unlike text (which the
# comms ingress bridge polls), a call is synchronous: Twilio POSTs the number's
# voice webhook and needs TwiML back in seconds, so calls need a live public
# webhook (a cloudflared tunnel to the local CM ingress) and a LiveKit Cloud SIP
# trunk for the media leg (the local `livekit-server --dev` used for browser meet
# has no SIP service). When disabled (default) the stack stays poll-only and the
# localhost numbers keep cleared voice webhooks. See selfhost/sync_comms_webhooks.py.
SELF_HOST_CALLS_ENABLED="${SELF_HOST_CALLS_ENABLED:-0}"

# The self-host compose bundle (entrypoints, fetch helpers) lives alongside this
# script in droid-deploy/deploy/selfhost/.
_SELF_HOST_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SELF_HOST_DEPLOY_SELFHOST_DIR="${SELF_HOST_DEPLOY_SELFHOST_DIR:-$_SELF_HOST_ENV_DIR/../deploy/selfhost}"

# Persistent self-host state (survives reboot; unlike /tmp).
SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-${DROID_HOME:-$HOME/.droid}}"

self_host_coordinator_runtime_file() {
  printf '%s/coordinator-runtime.json' "$SELF_HOST_STATE_DIR"
}

export_self_host_coordinator_runtime_file() {
  export SELF_HOST_COORDINATOR_RUNTIME_FILE="$(self_host_coordinator_runtime_file)"
  mkdir -p "$SELF_HOST_STATE_DIR"
}

default_self_host_workspace() {
  printf '%s' "${DROID_LOCAL_ROOT:-$SELF_HOST_DEFAULT_WORKSPACE}"
}

ensure_self_host_workspace_dir() {
  local workspace
  workspace="$(default_self_host_workspace)"
  mkdir -p "$workspace"
}

self_host_export_coordinator_contact_env() {
  # The local Coordinator uses fixed, shared contact identities so every
  # developer's localhost deployment converges on the same Twin contact rows.
  export DROID_COORDINATOR_EMAIL_ADDRESS="$SELF_HOST_COORDINATOR_EMAIL_ADDRESS"
  export ORCHESTRA_DROID_COORDINATOR_EMAIL_ADDRESS="${ORCHESTRA_DROID_COORDINATOR_EMAIL_ADDRESS:-$DROID_COORDINATOR_EMAIL_ADDRESS}"

  export DROID_COORDINATOR_PHONE_US="$SELF_HOST_COORDINATOR_PHONE_US"
  export ORCHESTRA_DROID_COORDINATOR_PHONE_US="${ORCHESTRA_DROID_COORDINATOR_PHONE_US:-$DROID_COORDINATOR_PHONE_US}"
  export DROID_COORDINATOR_DEFAULT_PHONE_COUNTRY="$SELF_HOST_COORDINATOR_DEFAULT_PHONE_COUNTRY"
  export ORCHESTRA_DROID_COORDINATOR_DEFAULT_PHONE_COUNTRY="${ORCHESTRA_DROID_COORDINATOR_DEFAULT_PHONE_COUNTRY:-$DROID_COORDINATOR_DEFAULT_PHONE_COUNTRY}"

  export DROID_COORDINATOR_PHONE="${SELF_HOST_COORDINATOR_PHONE:-$DROID_COORDINATOR_PHONE_US}"
  export ASSISTANT_NUMBER="$DROID_COORDINATOR_PHONE"
  export COMMS_BRIDGE_SMS_NUMBER="$DROID_COORDINATOR_PHONE"

  export DROID_COORDINATOR_WHATSAPP_NUMBER="$SELF_HOST_COORDINATOR_WHATSAPP_NUMBER"
  export ASSISTANT_WHATSAPP_NUMBER="$DROID_COORDINATOR_WHATSAPP_NUMBER"
  export COMMS_BRIDGE_WHATSAPP_NUMBER="$DROID_COORDINATOR_WHATSAPP_NUMBER"
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
    "DROID_COMMS_URL",
    "DROID_ADAPTERS_URL",
    # Self-host always runs with Console, so the stack forces DROID_CONSOLE_UI=on
    # (see stack.sh / service.sh). Ignore the public droid/.env value, which is
    # the headless-install default, so it can't clobber the stack's export.
    "DROID_CONSOLE_UI",
}
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

self_host_export_comms_sa() {
  # Internal-dev hosted Coordinator email: export the comms service-account key
  # so the gateway can send Coordinator email through the hosted Gmail mailbox
  # (and the comms ingress bridge can poll replies). The key is read only from
  # the self-host state dir (~/.droid by default) and never written to a repo.
  # No-op when absent, so the default fully-local stack is unchanged.
  local sa_file
  sa_file="${SELF_HOST_COMMS_SA_FILE:-${SELF_HOST_STATE_DIR:-${DROID_HOME:-$HOME/.droid}}/comms_sa.json}"
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
  twilio_file="${SELF_HOST_COMMS_TWILIO_FILE:-${SELF_HOST_STATE_DIR:-${DROID_HOME:-$HOME/.droid}}/comms_twilio.env}"
  [[ -f "$twilio_file" ]] || return 0
  load_self_host_env_file "$twilio_file"
  # Twilio credentials live in the local state file; Coordinator numbers come
  # from the canonical self-host contact env above.
  self_host_export_coordinator_contact_env
}

self_host_calls_enabled() {
  # True when the opt-in phone/WhatsApp call support is turned on.
  case "${SELF_HOST_CALLS_ENABLED:-0}" in
    1 | true | TRUE | yes | YES | on | ON) return 0 ;;
    *) return 1 ;;
  esac
}

self_host_livekit_cloud_file() {
  printf '%s' \
    "${SELF_HOST_LIVEKIT_CLOUD_FILE:-${SELF_HOST_STATE_DIR:-${DROID_HOME:-$HOME/.droid}}/livekit_cloud.env}"
}

self_host_export_livekit_cloud() {
  # Calls bridge Twilio -> LiveKit Cloud SIP, so they need a LiveKit Cloud
  # project (URL/key/secret + SIP URI), distinct from the local dev LiveKit used
  # for browser meet. The creds live in the self-host state dir (never a repo).
  # No-op unless calls are enabled and the file exists, so the default
  # browser-meet stack (local dev LiveKit) is untouched.
  self_host_calls_enabled || return 0
  local lk_file
  lk_file="$(self_host_livekit_cloud_file)"
  [[ -f "$lk_file" ]] || return 0
  load_self_host_env_file "$lk_file"
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

append_self_host_droid_runtime_env() {
  local -n _target_array="$1"
  local env_file="${2:-${SELF_HOST_ENV_FILE:-${DROID_ENV_FILE:-${DROID_REPO:-}/.env}}}"
  load_self_host_env_file "$env_file"

  local workspace
  workspace="$(default_self_host_workspace)"
  ensure_self_host_workspace_dir
  _target_array+=("DROID_LOCAL_ROOT=$workspace")
  _target_array+=(
    "DROID_CONVERSATION_LOCAL_COMMS_ENABLED=${DROID_CONVERSATION_LOCAL_COMMS_ENABLED:-true}"
    "DROID_CONVERSATION_LOCAL_COMMS_MODE=${DROID_CONVERSATION_LOCAL_COMMS_MODE:-local}"
    "DROID_CONVERSATION_LOCAL_COMMS_HOST=${DROID_CONVERSATION_LOCAL_COMMS_HOST:-127.0.0.1}"
    "DROID_CONVERSATION_LOCAL_COMMS_PORT=${DROID_CONVERSATION_LOCAL_COMMS_PORT:-8787}"
  )

  # Local deployment is a debugging surface like the test suite (rapid iteration,
  # breaking changes), so it adopts the test harness logging convention
  # (droid/tests/parallel_run.sh): cross-repo OTel spans from droid, unify, and
  # unillm aggregate into the droid repo's logs/all/ (one {trace_id}.jsonl per
  # run), while each repo's full file logs — including the LLM request/response
  # with reasoning — land in a per-repo logs/<repo>/ dir. All are opt-out: any
  # value exported beforehand wins. Orchestra runs as a separate process; its
  # equivalent dirs are set where it is launched (console start_orchestra).
  local _droid_repo_root="${DROID_REPO_PATH:-${DROID_REPO:-}}"
  if [[ -n "$_droid_repo_root" ]]; then
    local _otel_log_dir="${DROID_OTEL_LOG_DIR:-$_droid_repo_root/logs/all}"
    mkdir -p \
      "$_otel_log_dir" \
      "$_droid_repo_root/logs/droid" \
      "$_droid_repo_root/logs/unify" \
      "$_droid_repo_root/logs/unillm" 2>/dev/null || true
    _target_array+=(
      "DROID_OTEL=${DROID_OTEL:-true}"
      "UNIFY_OTEL=${UNIFY_OTEL:-true}"
      "UNILLM_OTEL=${UNILLM_OTEL:-true}"
      "DROID_OTEL_LOG_DIR=$_otel_log_dir"
      "UNIFY_OTEL_LOG_DIR=${UNIFY_OTEL_LOG_DIR:-$_otel_log_dir}"
      "UNILLM_OTEL_LOG_DIR=${UNILLM_OTEL_LOG_DIR:-$_otel_log_dir}"
      "DROID_LOG_DIR=${DROID_LOG_DIR:-$_droid_repo_root/logs/droid}"
      "UNIFY_LOG_DIR=${UNIFY_LOG_DIR:-$_droid_repo_root/logs/unify}"
      "UNILLM_LOG_DIR=${UNILLM_LOG_DIR:-$_droid_repo_root/logs/unillm}"
    )
  fi

  if [[ "${VOICE_PROVIDER:-}" == "elevenlabs" && -z "${VOICE_ID:-}" ]]; then
    _target_array+=("VOICE_ID=$SELF_HOST_COORDINATOR_VOICE_ID")
  fi

  # Phone & WhatsApp calls (opt-in): forward the LiveKit Cloud creds + SIP URI so
  # the persistent LiveKit worker registers with the cloud, and the public tunnel
  # URL (exported by stack.sh/service.sh once cloudflared is up) so the local
  # ingress reconstructs Twilio signature URLs and recording callbacks correctly.
  # No-op when calls are disabled. LiveKit creds are otherwise inherited from the
  # exported environment for the local dev server (browser meet).
  if self_host_calls_enabled; then
    # Load the LiveKit Cloud creds last so they win over any dev LIVEKIT_* that
    # load_self_host_env_file just pulled from droid/.env (voice.sh writes the
    # local dev pair there for browser meet).
    self_host_export_livekit_cloud
    local _call_key
    for _call_key in \
      LIVEKIT_URL \
      LIVEKIT_API_KEY \
      LIVEKIT_API_SECRET \
      LIVEKIT_SIP_URI; do
      if [[ -n "${!_call_key:-}" ]]; then
        _target_array+=("$_call_key=${!_call_key}")
      fi
    done
    if [[ -n "${DROID_CONVERSATION_LOCAL_COMMS_PUBLIC_URL:-}" ]]; then
      _target_array+=(
        "DROID_CONVERSATION_LOCAL_COMMS_PUBLIC_URL=${DROID_CONVERSATION_LOCAL_COMMS_PUBLIC_URL}"
      )
    fi
  fi

  local key val
  for key in \
    DROID_WEB_TAVILY_API_KEY \
    DROID_WEB_ENABLED \
    DROID_ACTOR_ANTICAPTCHA_KEY \
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
