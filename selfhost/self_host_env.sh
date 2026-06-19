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

load_self_host_env_file() {
  local env_file="${1:-}"
  if [[ -z "$env_file" || ! -f "$env_file" ]]; then
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
  if [[ "${VOICE_PROVIDER:-}" == "elevenlabs" && -z "${VOICE_ID:-}" ]]; then
    _target_array+=("VOICE_ID=$SELF_HOST_COORDINATOR_VOICE_ID")
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
