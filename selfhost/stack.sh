#!/usr/bin/env bash
# =============================================================================
# stack.sh — Self-host stack
# =============================================================================
#
# Brings up Orchestra, unity.gateway, Pub/Sub emulator, Console, and
# the Unity CM for the signed-in user's Coordinator when credentials exist.
#
# Usage:
#   ./scripts/stack.sh up           Fresh redeploy: purge local DB, seed, start, smoke
#   ./scripts/stack.sh up --durable Fresh redeploy in a persistent tmux session
#   ./scripts/stack.sh resume       Start/resume without resetting local history
#   ./scripts/stack.sh redeploy     Alias for up
#   ./scripts/stack.sh down [--full]    Stop stack (--full stops background runtime too)
#   ./scripts/stack.sh status       Show service status
#   ./scripts/stack.sh logs [svc]   Follow service logs (console|orchestra|pubsub|canvas)
#   ./scripts/stack.sh doctor       Check prerequisites
#   ./scripts/stack.sh smoke        Verify the running local product
#   ./scripts/stack.sh repair-console  Restart Console with preserved stack env
#   ./scripts/stack.sh reset        Purge local self-host onboarding/chat history
#   ./scripts/stack.sh dev-env      Print non-secret Console env expected by stack
#   ./scripts/stack.sh sync-comms [--check|--set-voice|--revert-voice]
#                                   Reconcile localhost Twilio webhooks (text
#                                   poll-only; voice -> tunnel when calls enabled)
#
# Environment:
#   UNIFY_STACK_ROOT          Parent dir with orchestra/console/unify siblings
#   OPENAI_API_KEY / ANTHROPIC_API_KEY  Required for Coordinator chat
#   DEEPGRAM_API_KEY / CARTESIA_API_KEY Required for browser calls (prompted by unity setup)
#   SELF_HOST_CALLS_ENABLED=0 Disable inbound/outbound phone & WhatsApp calls.
#                             Calls default on and need LiveKit Cloud SIP creds
#                             in ~/.unity/livekit_cloud.env plus cloudflared.
#                             Startup brings up a tunnel + SIP trunk and points
#                             the localhost number's voice webhook at the local CM.
#   SELF_HOST_PROVIDER_TRIGGERS_ENABLED=1 Enable provider-event triggers on
#                             the source stack (unity/.env or ~/.unity/.env).
#                             Requires public HTTPS callback base, Composio keys,
#                             wrapping master key, and a BYO reverse proxy to
#                             Orchestra's webhook route. Starts the trigger worker.
#
set -euo pipefail

# This script lives in unity-deploy/selfhost/. The self-host stack orchestrates
# the sibling unify, console, and orchestra checkouts located under
# UNIFY_STACK_ROOT (defaults to the parent of unity-deploy).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=default_repo_paths.sh
source "$SCRIPT_DIR/default_repo_paths.sh"
DEPLOY_REPO_PATH="$(cd "$SCRIPT_DIR/.." && pwd -P)"
ENSURE_PREREQS_SCRIPT="$SCRIPT_DIR/ensure_prereqs.sh"
SELF_HOST_ENV_SCRIPT="$SCRIPT_DIR/self_host_env.sh"
STACK_STATE_SCRIPT="$SCRIPT_DIR/stack_state.sh"
RESET_DB_SCRIPT="$SCRIPT_DIR/reset_db.sh"
# shellcheck source=canvas_origin.sh
source "$SCRIPT_DIR/canvas_origin.sh"

UNIFY_STACK_ROOT="${UNIFY_STACK_ROOT:-$(cd "$DEPLOY_REPO_PATH/.." && pwd -P)}"
UNITY_REPO_PATH="${UNITY_REPO_PATH:-$(default_unity_repo_path "$UNIFY_STACK_ROOT")}"
export UNITY_REPO_PATH
CONSOLE_REPO_PATH="${CONSOLE_REPO_PATH:-$UNIFY_STACK_ROOT/console}"
ORCHESTRA_REPO_PATH="${ORCHESTRA_REPO_PATH:-$UNIFY_STACK_ROOT/orchestra}"

CONSOLE_LOCAL_SCRIPT="$CONSOLE_REPO_PATH/scripts/local.sh"

if [[ -f "$STACK_STATE_SCRIPT" ]]; then
  # shellcheck disable=SC1090
  source "$STACK_STATE_SCRIPT"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }

require_repo() {
  local label="$1"
  local path="$2"
  if [[ ! -d "$path" ]]; then
    log_error "$label repo not found at: $path"
    log_info "Clone sibling repos under UNIFY_STACK_ROOT or set ${label}_REPO_PATH"
    return 1
  fi
}

_has_env_key() {
  local key="$1"
  local env_file="$UNITY_REPO_PATH/.env"
  [[ -n "${!key:-}" ]] && return 0
  [[ -f "$env_file" ]] && grep -qE "^${key}=.+$" "$env_file"
}

default_orchestra_db_port() {
  if command -v docker &>/dev/null; then
    local mapped
    mapped="$(docker port orchestra-local-db 5432/tcp 2>/dev/null | head -1 | sed 's/.*://')"
    if [[ -z "$mapped" ]]; then
      mapped="$(docker inspect -f '{{(index (index .HostConfig.PortBindings "5432/tcp") 0).HostPort}}' orchestra-local-db 2>/dev/null || true)"
    fi
    if [[ -n "$mapped" ]]; then
      printf '%s' "$mapped"
      return 0
    fi
  fi
  if [[ -n "${ORCHESTRA_DB_PORT:-}" ]]; then
    printf '%s' "$ORCHESTRA_DB_PORT"
    return 0
  fi
  printf '55432'
}

cleanup_legacy_orchestra_launch_job() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    return 0
  fi
  if ! command -v launchctl &>/dev/null; then
    return 0
  fi
  if launchctl list 2>/dev/null | grep -q '[[:space:]]orchestra-local-dev$'; then
    log_warn "Removing legacy orchestra-local-dev launch job before stack start"
    launchctl remove orchestra-local-dev 2>/dev/null || true
  fi
}

cmd_doctor() {
  local ok=true
  echo ""
  echo "Self-host doctor"
  echo "================"
  echo ""
  echo "Stranger path: curl install → unity setup → unity → register → chat"
  echo ""

  echo "Infrastructure"
  echo "--------------"

  if ! command -v docker &>/dev/null; then
    log_error "Docker is not installed"
    ok=false
  elif ! docker info &>/dev/null; then
    log_error "Docker daemon is not running"
    ok=false
  else
    log_success "Docker is available"
  fi

  if [[ -f "$ENSURE_PREREQS_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$ENSURE_PREREQS_SCRIPT"
    if ! ensure_node; then
      ok=false
    else
      log_success "Node.js/npm ready"
    fi
    if ! ensure_java; then
      ok=false
    else
      log_success "Java JRE ready"
    fi
    if ! ensure_pubsub_emulator; then
      ok=false
    else
      log_success "Pub/Sub emulator ready"
    fi
    if [[ "${SELF_HOST_DESKTOP:-0}" == "1" ]]; then
      if ! ensure_rclone; then
        ok=false
      else
        log_success "rclone ready (desktop file sync)"
      fi
    fi
  else
    log_warn "ensure_prereqs.sh missing — checking Java/gcloud manually"
    if ! command -v gcloud &>/dev/null; then
      ok=false
      log_error "gcloud CLI not found"
    fi
    if ! command -v java &>/dev/null || ! java -version &>/dev/null 2>&1; then
      ok=false
      log_error "Java JRE required for Pub/Sub emulator"
    fi
  fi

  echo ""
  echo "Sibling repos"
  echo "-------------"
  require_repo "Console" "$CONSOLE_REPO_PATH" || ok=false
  require_repo "Orchestra" "$ORCHESTRA_REPO_PATH" || ok=false

  if [[ -f "$UNITY_REPO_PATH/.venv/bin/python" ]]; then
    local unity_py="$UNITY_REPO_PATH/.venv/bin/python"
    if "$unity_py" -c "import unify.gateway" &>/dev/null; then
      log_success "Unity venv + unify.gateway OK"
    else
      log_error "unify.gateway not importable — run: cd $UNITY_REPO_PATH && uv sync"
      ok=false
    fi
  else
    log_warn "Unity .venv missing — run: cd $UNITY_REPO_PATH && uv sync"
    ok=false
  fi

  if [[ -f "$CONSOLE_REPO_PATH/.env.local" ]]; then
    log_success "console/.env.local found"
  else
    log_warn "console/.env.local missing — copy from .env.development and set JWT_SECRET"
    ok=false
  fi

  echo ""
  echo "BYOK keys (unity/.env)"
  echo "----------------------"
  echo "  Required: LLM (OpenAI or Anthropic)"
  echo "  Voice:    LiveKit Cloud + Deepgram + Cartesia/ElevenLabs"
  echo "  Optional: Tavily (web search), AntiCaptcha (computer use)"
  echo ""

  if _has_env_key OPENAI_API_KEY || _has_env_key ANTHROPIC_API_KEY; then
    log_success "LLM provider key configured"
  else
    log_error "No LLM API key — run: unity setup (or scripts/prompt_byok_keys.sh)"
    ok=false
  fi

  if _has_env_key DEEPGRAM_API_KEY; then
    log_success "DEEPGRAM_API_KEY set"
  else
    log_warn "DEEPGRAM_API_KEY missing — browser calls need STT"
  fi

  if _has_env_key CARTESIA_API_KEY || _has_env_key ELEVEN_API_KEY; then
    log_success "Text-to-speech key set (Cartesia or ElevenLabs)"
  else
    log_warn "No TTS key — browser calls need CARTESIA_API_KEY or ELEVEN_API_KEY"
  fi

  if _has_env_key UNITY_WEB_TAVILY_API_KEY; then
    log_success "UNITY_WEB_TAVILY_API_KEY set (web search)"
  else
    log_info "Web search not configured (optional — Tavily via prompt_byok_keys.sh)"
  fi

  if _has_env_key ANTICAPTCHA_KEY || _has_env_key UNITY_ACTOR_ANTICAPTCHA_KEY; then
    log_success "AntiCaptcha key set (computer automation)"
  else
    log_info "AntiCaptcha not configured (optional — computer use / CAPTCHA solving)"
  fi

  echo ""
  echo "Runtime"
  echo "-------"
  log_info "FileManager workspace: ${UNITY_LOCAL_ROOT:-$HOME/Unity/Local}"
  log_info "Scheduled tasks: LocalActivationScheduler in Coordinator CM"
  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    self_host_runtime_doctor_line | sed 's/^/  /'
    if declare -F self_host_export_livekit_backend &>/dev/null; then
      self_host_export_livekit_backend
    fi
    if declare -F self_host_livekit_media_configured &>/dev/null \
      && self_host_livekit_media_configured; then
      log_success "LiveKit Cloud media credentials configured ($(self_host_livekit_cloud_file))"
    else
      log_error "Missing LiveKit Cloud media credentials — add LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET to $(self_host_livekit_cloud_file)"
      ok=false
    fi
    if declare -F self_host_calls_enabled &>/dev/null \
      && self_host_calls_enabled; then
      if declare -F self_host_livekit_sip_configured &>/dev/null \
        && self_host_livekit_sip_configured; then
        log_success "LiveKit Cloud SIP URI configured"
      else
        log_error "Missing LIVEKIT_SIP_URI in $(self_host_livekit_cloud_file) — phone and WhatsApp calls require LiveKit Cloud SIP"
        ok=false
      fi
    fi
    echo ""
    log_info "Daily driver: unity stack up / unity stack down"
    log_info "Stop everything: unity stack down --full  (or: unity service disable)"
    log_info "Survive reboot without Console: unity setup --boot-runtime"
  else
    log_info "Stack must stay up for scheduled tasks until self-host runtime is wired"
  fi
  log_info "Live Actions stream via EventBus → Pub/Sub actions-sub"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    load_self_host_repo_env_file "$UNITY_REPO_PATH/.env"
    if declare -F self_host_load_state_env_overlay &>/dev/null; then
      self_host_load_state_env_overlay
    fi
  fi

  if declare -F self_host_provider_triggers_enabled &>/dev/null \
    && self_host_provider_triggers_enabled; then
    echo ""
    echo "Provider-event triggers"
    echo "-----------------------"
    if self_host_validate_provider_trigger_config; then
      log_success "Provider-trigger prerequisites configured"
      if self_host_provider_trigger_worker_is_healthy; then
        log_success "Provider-trigger worker healthy (port $(self_host_provider_trigger_worker_port))"
      elif self_host_provider_trigger_worker_is_running; then
        log_warn "Provider-trigger worker running but not ready"
      else
        log_warn "Provider-trigger worker not running — starts on stack up"
      fi
      log_info "Callback base: ${ORCHESTRA_TRIGGER_CALLBACK_BASE_URL:-?}"
      log_info "Proxy webhooks to http://127.0.0.1:${ORCHESTRA_PORT:-8000}/v0/webhooks/integrations/*"
    else
      log_error "Provider-trigger configuration incomplete"
      ok=false
    fi
  fi

  echo ""
  if [[ "$ok" == "true" ]]; then
    log_success "Doctor passed — run: unity stack up"
    return 0
  fi
  log_error "Doctor found blockers — fix above, then re-run: unity stack doctor"
  return 1
}

PACKAGED_SELFHOST_DIR="$DEPLOY_REPO_PATH/deploy/selfhost"
SYNC_COMMS_SCRIPT="$PACKAGED_SELFHOST_DIR/sync_comms_webhooks.py"

# Enforce that the localhost Twilio numbers are "poll-only" (no hosted inbound
# webhook), so a hosted backend never answers localhost traffic. Reads the
# localhost numbers from self_host_env.sh and Twilio creds from the env /
# ~/.unity/comms_twilio.env. No-op when the script or creds are absent.
cmd_sync_comms() {
  [[ -f "$SYNC_COMMS_SCRIPT" ]] || { log_warn "Missing $SYNC_COMMS_SCRIPT"; return 0; }
  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    if declare -F self_host_export_comms_twilio &>/dev/null; then
      self_host_export_comms_twilio
    fi
  fi
  python3 "$SYNC_COMMS_SCRIPT" "$@"
}

# Select the LiveKit backend for the source stack. Browser Meet, phone, and
# WhatsApp calls all use the same LiveKit Cloud project.
setup_livekit_env() {
  if declare -F self_host_export_livekit_backend &>/dev/null; then
    self_host_export_livekit_backend
  fi
  if ! declare -F self_host_livekit_media_configured &>/dev/null \
    || ! self_host_livekit_media_configured; then
    log_error "LiveKit Cloud media credentials are required for the source stack"
    log_info "Add LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET to $(self_host_livekit_cloud_file)"
    return 1
  fi
  if declare -F self_host_calls_enabled &>/dev/null \
    && self_host_calls_enabled \
    && [[ -z "${LIVEKIT_SIP_URI:-}" ]]; then
    log_error "LIVEKIT_SIP_URI is required when phone/WhatsApp calls are enabled"
    log_info "Add it to $(self_host_livekit_cloud_file)"
    return 1
  fi
}

# Best-effort, non-fatal drift warning used during `up`. Only runs when WhatsApp
# creds are present; never blocks or fails startup. Skipped when calls are
# enabled — cmd_up_calls_setup then sets the voice webhook authoritatively.
warn_if_comms_webhooks_drift() {
  [[ -f "$SYNC_COMMS_SCRIPT" ]] || return 0
  if declare -F self_host_calls_enabled &>/dev/null && self_host_calls_enabled; then
    return 0
  fi
  [[ -n "${TWILIO_WA_ACCOUNT_SID:-}" && -n "${TWILIO_WA_AUTH_TOKEN:-}" ]] || return 0
  if ! python3 "$SYNC_COMMS_SCRIPT" --check >/dev/null 2>&1; then
    log_warn "A localhost Twilio number still has a hosted inbound webhook —"
    log_warn "inbound replies may be answered by staging/prod. Run: $0 sync-comms"
  fi
}

# Bring up provider-event triggers when the opt-in toggle is enabled. Operators
# must supply a public HTTPS callback base (BYO reverse proxy); Unify does not
# run a managed webhook tunnel for provider triggers.
cmd_up_provider_triggers_setup() {
  if ! declare -F self_host_provider_triggers_enabled &>/dev/null \
    || ! self_host_provider_triggers_enabled; then
    return 0
  fi
  log_info "Enabling provider-event triggers (worker + private blob storage)..."

  if ! self_host_validate_provider_trigger_config; then
    log_error "Provider-trigger configuration is incomplete"
    return 1
  fi
  self_host_export_provider_trigger_env

  if ! self_host_ensure_provider_trigger_worker; then
    log_error "Provider-trigger worker failed to start"
    log_info "Logs: $(self_host_provider_trigger_worker_log_file)"
    return 1
  fi
  log_success "Provider-trigger worker ready on port $(self_host_provider_trigger_worker_port)"
  log_info "Point your HTTPS reverse proxy at http://127.0.0.1:${ORCHESTRA_PORT:-8000}/v0/webhooks/integrations/*"
  log_info "Composio callback base: ${ORCHESTRA_TRIGGER_CALLBACK_BASE_URL}"
}

# Bring up the inbound-call edge: the cloudflared tunnel to the local CM ingress,
# the LiveKit Cloud inbound SIP trunk, and the Twilio voice webhook pointing at
# the tunnel (text stays poll-only). No-op when calls are explicitly disabled.
cmd_up_calls_setup() {
  if ! declare -F self_host_calls_enabled &>/dev/null || ! self_host_calls_enabled; then
    return 0
  fi
  log_info "Enabling phone/WhatsApp calls (LiveKit Cloud SIP + tunnel)..."

  if [[ -f "$ENSURE_PREREQS_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$ENSURE_PREREQS_SCRIPT"
    if declare -F ensure_cloudflared &>/dev/null; then
      if ! ensure_cloudflared; then
        log_error "cloudflared unavailable — cannot expose local call webhooks"
        return 1
      fi
    fi
  fi

  if ! declare -F self_host_ensure_tunnel &>/dev/null || ! self_host_ensure_tunnel; then
    log_error "Call tunnel failed to start — cannot expose local call webhooks"
    return 1
  fi
  log_success "Call tunnel: ${UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL:-?}"

  local py="$UNITY_REPO_PATH/.venv/bin/python"
  [[ -x "$py" ]] || py="python3"

  if [[ ! -f "$PACKAGED_SELFHOST_DIR/provision_call_sip.py" ]]; then
    log_error "Missing $PACKAGED_SELFHOST_DIR/provision_call_sip.py"
    return 1
  fi
  if ! "$py" "$PACKAGED_SELFHOST_DIR/provision_call_sip.py"; then
    log_error "LiveKit SIP trunk provisioning failed — inbound calls cannot route"
    return 1
  fi

  if [[ ! -f "$SYNC_COMMS_SCRIPT" ]]; then
    log_error "Missing $SYNC_COMMS_SCRIPT"
    return 1
  fi
  if declare -F self_host_export_comms_twilio &>/dev/null; then
    self_host_export_comms_twilio
  fi
  if "$py" "$SYNC_COMMS_SCRIPT" --set-voice; then
    if declare -F self_host_voice_synced_url_file &>/dev/null; then
      printf '%s' "${UNITY_CONVERSATION_LOCAL_COMMS_PUBLIC_URL}" \
        >"$(self_host_voice_synced_url_file)"
    fi
  else
    log_error "Voice webhook sync failed — inbound calls may be answered by staging/prod"
    return 1
  fi
}

current_tmux_session() {
  [[ -n "${TMUX:-}" ]] || return 1
  tmux display-message -p '#S' 2>/dev/null || return 1
}

stop_durable_stack_session() {
  local session="${UNITY_STACK_TMUX_SESSION:-unity-stack}"
  command -v tmux &>/dev/null || return 0
  tmux has-session -t "=${session}" 2>/dev/null || return 0

  local current=""
  current="$(current_tmux_session 2>/dev/null || true)"
  if [[ "$current" == "$session" ]]; then
    return 0
  fi

  log_info "Clearing durable stack session: $session"
  tmux kill-session -t "=${session}" 2>/dev/null || true
}

cmd_purge_fresh_redeploy_state() {
  export SELF_HOST=1
  export UNITY_HOME="${UNITY_HOME:-$HOME/.unity}"
  export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$UNITY_HOME}"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
  fi

  local orchestra_local_script="$ORCHESTRA_REPO_PATH/scripts/local.sh"
  if [[ ! -f "$orchestra_local_script" ]]; then
    log_error "Missing $orchestra_local_script"
    return 1
  fi

  local state_dir="${SELF_HOST_STATE_DIR:-$UNITY_HOME}"
  log_info "Clearing self-host runtime identity state in $state_dir"
  local stale_state_files=(
    "coordinator-runtime.json"
    "self-host-owner.json"
    "runtime-state.json"
    "runtime.lock"
    "full-stack-state.json"
  )
  local file
  for file in "${stale_state_files[@]}"; do
    rm -f "$state_dir/$file"
  done

  log_info "Purging local Orchestra database for a fresh self-host redeploy"
  ORCHESTRA_ALLOW_ISOLATED=1 bash "$orchestra_local_script" purge
}

read_env_value() {
  local key="$1"
  shift
  local file value
  for file in "$@"; do
    [[ -f "$file" ]] || continue
    value="$(grep -E "^${key}=" "$file" 2>/dev/null | sed 's/^[^=]*=//' | tr -d '"' || true)"
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return 0
    fi
  done
  printf ''
}

console_admin_key() {
  local key=""
  key="$(read_env_value ORCHESTRA_ADMIN_KEY \
    "$CONSOLE_REPO_PATH/.env.local" \
    "$CONSOLE_REPO_PATH/.env.development" \
    "$CONSOLE_REPO_PATH/.env")"
  printf '%s' "${key:-local-admin-key}"
}

runtime_json_value() {
  local runtime_file="$1"
  shift
  [[ -f "$runtime_file" ]] || return 1
  python3 - "$runtime_file" "$@" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
for key in sys.argv[2:]:
    value = data.get(key)
    if value:
        print(value)
        raise SystemExit(0)
raise SystemExit(1)
PY
}

cmd_seed_builtins() {
  export SELF_HOST=1
  export UNITY_HOME="${UNITY_HOME:-$HOME/.unity}"
  export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$UNITY_HOME}"
  export ORCHESTRA_PORT="${ORCHESTRA_PORT:-8000}"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    export_self_host_coordinator_runtime_file
    load_self_host_repo_env_file "$UNITY_REPO_PATH/.env"
  fi

  local runtime_file="${SELF_HOST_COORDINATOR_RUNTIME_FILE:-$UNITY_HOME/coordinator-runtime.json}"
  local api_key=""
  api_key="$(runtime_json_value "$runtime_file" apiKey api_key 2>/dev/null || true)"
  if [[ -z "$api_key" ]]; then
    api_key="$(console_admin_key)"
    log_info "Seeding Builtins catalogues with local Orchestra admin key"
  fi

  local py="$UNITY_REPO_PATH/.venv/bin/python"
  [[ -x "$py" ]] || py="python3"
  if [[ ! -f "$UNITY_REPO_PATH/scripts/seed_builtins_catalog.py" ]]; then
    log_error "Missing $UNITY_REPO_PATH/scripts/seed_builtins_catalog.py"
    return 1
  fi

  local args=()
  local manifest="$DEPLOY_REPO_PATH/deploy/selfhost/integration-bootstrap.selfhost.toml"
  if [[ -n "${COMPOSIO_API_KEY:-}" && -f "$manifest" ]]; then
    args+=(--integration-bootstrap-manifest "$manifest")
    export UNITY_INTEGRATION_BOOTSTRAP_EXECUTOR="${UNITY_INTEGRATION_BOOTSTRAP_EXECUTOR:-direct_worker}"
    export ORCHESTRA_ADMIN_KEY="${ORCHESTRA_ADMIN_KEY:-$(console_admin_key)}"
    # The direct worker imports orchestra, which lives in the sibling
    # checkout's venv — not in unify's.
    local orchestra_py="$ORCHESTRA_REPO_PATH/.venv/bin/python"
    if [[ -x "$orchestra_py" ]]; then
      export UNITY_INTEGRATION_BOOTSTRAP_DIRECT_WORKER_CMD="${UNITY_INTEGRATION_BOOTSTRAP_DIRECT_WORKER_CMD:-$orchestra_py -m orchestra.workers.builtins_artifacts_seed_job}"
    fi
  fi

  log_info "Seeding Builtins catalogues..."
  (
    cd "$UNITY_REPO_PATH"
    UNIFY_KEY="$api_key" \
      ORCHESTRA_URL="http://127.0.0.1:${ORCHESTRA_PORT}/v0" \
      ORCHESTRA_DB_HOST="${ORCHESTRA_DB_HOST:-127.0.0.1}" \
      ORCHESTRA_DB_PORT="${ORCHESTRA_DB_PORT:-$(default_orchestra_db_port)}" \
      ORCHESTRA_DB_USER="${ORCHESTRA_DB_USER:-orchestra}" \
      ORCHESTRA_DB_PASS="${ORCHESTRA_DB_PASS:-orchestra}" \
      ORCHESTRA_DB_BASE="${ORCHESTRA_DB_BASE:-orchestra}" \
      "$py" scripts/seed_builtins_catalog.py "${args[@]}"
  )
}

wait_for_http() {
  local label="$1"
  local url="$2"
  local timeout_seconds="${3:-60}"
  local elapsed=0
  while (( elapsed < timeout_seconds )); do
    if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
      log_success "$label ready: $url"
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  log_error "$label did not become ready: $url"
  return 1
}

wait_for_coordinator_runtime() {
  local timeout_seconds="${1:-90}"
  local elapsed=0
  while (( elapsed < timeout_seconds )); do
    if declare -F unity_cm_instance_count &>/dev/null \
      && [[ "$(unity_cm_instance_count)" -eq 1 ]]; then
      log_success "Coordinator runtime ready"
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  log_error "Coordinator runtime did not become ready"
  return 1
}

wait_for_source_stack_ready() {
  local console_port="${CONSOLE_PORT:-3000}"
  local orchestra_port="${ORCHESTRA_PORT:-8000}"
  local gateway_host="${UNITY_GATEWAY_HOST:-127.0.0.1}"
  local gateway_port="${UNITY_GATEWAY_PORT:-8001}"

  wait_for_http "Console" "http://127.0.0.1:${console_port}" 90
  wait_for_http "Orchestra" "http://127.0.0.1:${orchestra_port}/v0/features" 90
  wait_for_http "Unity gateway" "http://${gateway_host}:${gateway_port}/health" 90
  if [[ -f "${SELF_HOST_COORDINATOR_RUNTIME_FILE:-}" ]]; then
    wait_for_coordinator_runtime 90
  else
    log_info "Coordinator runtime pending signup"
  fi
}

check_account_page() {
  local console_port="${CONSOLE_PORT:-3000}"
  local status=""
  status="$(curl -sS -o /dev/null -w '%{http_code}' -L --max-time 20 \
    "http://localhost:${console_port}/account" || true)"
  if [[ "$status" == "200" ]]; then
    log_success "Account page reachable: http://localhost:${console_port}/account"
  else
    log_error "Account page check failed: HTTP ${status:-000}"
    return 1
  fi

  local node_bin="${NODE_BIN:-node}"
  local browser_smoke_script="$SCRIPT_DIR/account_browser_smoke.mjs"
  if [[ ! -f "$browser_smoke_script" ]]; then
    log_error "Missing browser smoke script: $browser_smoke_script"
    return 1
  fi
  if ! command -v "$node_bin" >/dev/null 2>&1; then
    log_error "Node.js is required for authenticated browser smoke checks"
    return 1
  fi

  CONSOLE_REPO_PATH="$CONSOLE_REPO_PATH" \
    CONSOLE_PORT="$console_port" \
    UNITY_HOME="${UNITY_HOME:-$HOME/.unity}" \
    SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-${UNITY_HOME:-$HOME/.unity}}" \
    "$node_bin" "$browser_smoke_script"
}

cmd_redeploy() {
  echo ""
  echo "=============================================="
  echo "  Fresh self-host redeploy"
  echo "=============================================="
  echo ""

  if declare -F stack_state_refuse_if_compose_active &>/dev/null; then
    stack_state_refuse_if_compose_active || return 1
  fi

  stop_durable_stack_session
  cmd_down --full || true

  cmd_purge_fresh_redeploy_state
  cmd_resume
  cmd_reset --yes
  cmd_seed_builtins

  if [[ -f "$CONSOLE_LOCAL_SCRIPT" && -f "${SELF_HOST_COORDINATOR_RUNTIME_FILE:-}" ]]; then
    UNITY_ALLOW_RUNTIME_STOP=1 SELF_HOST=1 bash "$CONSOLE_LOCAL_SCRIPT" stop-runtime-backend >/dev/null 2>&1 || true
    SELF_HOST=1 bash "$CONSOLE_LOCAL_SCRIPT" start-runtime-backend --self-host
  fi

  wait_for_source_stack_ready
  cmd_smoke
  if [[ -f "${SELF_HOST_COORDINATOR_RUNTIME_FILE:-}" ]]; then
    check_account_page
  else
    log_info "Authenticated browser smoke pending signup"
  fi

  echo ""
  cmd_status
}

cmd_resume() {
  echo ""
  echo "=============================================="
  echo "  Starting self-host stack"
  echo "=============================================="
  echo ""

  if declare -F stack_state_refuse_if_compose_active &>/dev/null; then
    stack_state_refuse_if_compose_active || return 1
  fi

  cleanup_legacy_orchestra_launch_job

  if ! cmd_doctor; then
    log_error "Fix doctor findings before running stack up"
    return 1
  fi

  if [[ ! -f "$CONSOLE_LOCAL_SCRIPT" ]]; then
    log_error "Missing $CONSOLE_LOCAL_SCRIPT"
    return 1
  fi

  export SELF_HOST=1
  export DEPLOY_REPO_PATH
  export ORCHESTRA_REPO_PATH
  export UNITY_REPO_PATH
  export CONSOLE_REPO_PATH
  export ORCHESTRA_DB_PORT="$(default_orchestra_db_port)"
  export UNITY_HOME="${UNITY_HOME:-$HOME/.unity}"
  export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$UNITY_HOME}"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    export_self_host_coordinator_runtime_file
    load_self_host_repo_env_file "$UNITY_REPO_PATH/.env"
    if declare -F self_host_load_state_env_overlay &>/dev/null; then
      self_host_load_state_env_overlay
    fi
    if declare -F self_host_export_provider_trigger_env &>/dev/null; then
      self_host_export_provider_trigger_env
    fi
    if declare -F self_host_enable_runtime &>/dev/null; then
      self_host_enable_runtime
    fi
  fi

  # Internal-dev hosted Coordinator email: load the comms service-account key
  # (from ~/.unity, never a repo) so the gateway can send Coordinator email via
  # the hosted Gmail mailbox. The comms ingress bridge that polls replies is
  # started by the runtime supervisor. No-op when no key is present.
  if declare -F self_host_export_comms_sa &>/dev/null; then
    self_host_export_comms_sa
  fi
  if declare -F self_host_export_comms_twilio &>/dev/null; then
    self_host_export_comms_twilio
  fi

  # Warn (don't mutate) if a localhost number drifted back to a hosted webhook.
  warn_if_comms_webhooks_drift

  # Self-host always runs with Console, so the Coordinator onboarding flow
  # (narration + reference quiz) must stay active even though the public unity
  # default (unity/.env) disables it for headless installs.
  export UNITY_CONSOLE_UI=true

  # The self-host CM is the personal Coordinator; surface its universal email
  # (and provider) the way the hosted assignment event would, so outbound
  # Coordinator mail + the reference quiz work. No-op until a Coordinator
  # mailbox is configured.
  if [[ -n "${UNITY_COORDINATOR_EMAIL_ADDRESS:-}" ]]; then
    export ASSISTANT_EMAIL="${UNITY_COORDINATOR_EMAIL_ADDRESS}"
    export ASSISTANT_EMAIL_PROVIDER="${ASSISTANT_EMAIL_PROVIDER:-google_workspace}"
  fi

  setup_livekit_env || return 1

  # Bring up the inbound-call edge before Console starts the CM, so the CM
  # inherits the public tunnel URL and the voice webhook points at it. No-op
  # when calls are disabled.
  cmd_up_calls_setup

  cmd_up_provider_triggers_setup || return 1

  # Before Console: it reads CANVAS_ORIGIN at startup to build its CSP `frame-src`,
  # so starting the origin afterwards would leave the running Console unable to
  # frame it until the next restart.
  canvas_origin_start

  if declare -F self_host_ensure_service_supervisor &>/dev/null \
    && [[ -f "$SCRIPT_DIR/service.sh" ]]; then
    log_info "Ensuring background runtime (scheduled tasks while stack is down)..."
    if ! self_host_ensure_service_supervisor "$SCRIPT_DIR/service.sh"; then
      log_warn "Background runtime failed to start — stack down will stop scheduled tasks"
    fi
  fi

  if ! bash "$CONSOLE_LOCAL_SCRIPT" start --self-host; then
    log_error "Self-host stack failed to start"
    return 1
  fi

  local runtime_file="${SELF_HOST_COORDINATOR_RUNTIME_FILE:-}"

  if [[ -f "$runtime_file" ]]; then
    local cm_count="0"
    if declare -F unity_cm_instance_count &>/dev/null; then
      cm_count="$(unity_cm_instance_count)"
    fi
    if [[ "$cm_count" -eq 1 ]]; then
      if declare -F self_host_adopt_coordinator_for_service &>/dev/null; then
        self_host_adopt_coordinator_for_service "${SELF_HOST_COORDINATOR_AGENT_ID:-}" || true
      fi
      if ! bash "$CONSOLE_LOCAL_SCRIPT" ensure-coordinator-topics; then
        log_warn "Coordinator Pub/Sub setup failed — sign in at Console to refresh credentials"
      fi
      log_success "Reusing Coordinator runtime"
    elif [[ "$cm_count" -gt 1 ]]; then
      log_error "Multiple Coordinator runtimes detected — run: unity stack down --full"
    else
      log_info "Starting Coordinator runtime (saved login)..."
      if ! bash "$CONSOLE_LOCAL_SCRIPT" ensure-coordinator-topics; then
        log_warn "Coordinator Pub/Sub setup failed — sign in at Console to refresh credentials"
      elif declare -F with_unity_runtime_start_lock &>/dev/null; then
        if ! with_unity_runtime_start_lock 30 bash "$CONSOLE_LOCAL_SCRIPT" start-coordinator; then
          log_warn "Coordinator start failed — sign in at Console to refresh credentials"
        else
          log_success "Coordinator runtime is ready"
        fi
      elif ! bash "$CONSOLE_LOCAL_SCRIPT" start-coordinator; then
        log_warn "Coordinator start failed — sign in at Console to refresh credentials"
      else
        log_success "Coordinator runtime is ready"
      fi
    fi
  fi

  if declare -F stack_state_write_source &>/dev/null; then
    stack_state_write_source
  fi

  local console_port="${CONSOLE_PORT:-3000}"
  echo ""
  echo "=============================================="
  log_success "Self-host stack is ready"
  echo "=============================================="
  echo ""
  echo "  Console:   http://localhost:${console_port}"
  if canvas_origin_is_running; then
    echo "  Canvas:    ${CANVAS_ORIGIN}"
  fi
  echo ""
  if [[ -f "$runtime_file" ]]; then
    echo "  Open Console and chat with your Coordinator."
    if declare -F self_host_headless_scheduling_ready &>/dev/null \
      && self_host_headless_scheduling_ready; then
      echo "  stack down stops the UI only — scheduled tasks keep running in the background."
    elif declare -F self_host_service_is_enabled &>/dev/null \
      && self_host_service_is_enabled; then
      echo "  Background runtime is not healthy — stack down stops scheduled tasks."
      echo "  Re-run: unity stack up"
    fi
  else
    echo "  First visit: create an account on /login — Coordinator starts automatically."
  fi
  echo ""
}

cmd_up_durable() {
  local session="${UNITY_STACK_TMUX_SESSION:-unity-stack}"
  local timeout_seconds="${UNITY_STACK_TMUX_READY_TIMEOUT_SECONDS:-420}"
  local console_port="${CONSOLE_PORT:-3000}"
  local bash_bin="${UNITY_STACK_BASH:-bash}"

  if [[ "$(uname -s)" == "Darwin" && -x "/opt/homebrew/bin/bash" ]]; then
    bash_bin="/opt/homebrew/bin/bash"
  elif ! command -v "$bash_bin" &>/dev/null; then
    log_error "Bash not found: $bash_bin"
    return 1
  fi

  if ! command -v tmux &>/dev/null; then
    log_error "tmux is required for durable stack startup"
    log_info "Install tmux or run stack up from a long-lived human terminal"
    return 1
  fi

  if tmux has-session -t "=${session}" 2>/dev/null; then
    local current=""
    current="$(current_tmux_session 2>/dev/null || true)"
    if [[ "$current" == "$session" ]]; then
      log_warn "Already inside durable stack session: $session"
      cmd_status || true
      return 0
    fi
    log_warn "Replacing existing durable stack session: $session"
    tmux kill-session -t "=${session}" 2>/dev/null || true
  fi

  local shell_bin="${SHELL:-/bin/bash}"
  # tmux new-session inherits the tmux server's env (not this client's), so opt-in
  # toggles like the call-support gate must be injected into the command itself.
  local inner="export PATH=\"/opt/homebrew/bin:\$PATH\"; cd \"$DEPLOY_REPO_PATH\"; \"$bash_bin\" \"$SCRIPT_DIR/stack.sh\" up; rc=\$?; echo __UNITY_STACK_UP_EXIT_\${rc}__; exec \"$shell_bin\" -l"
  if [[ -n "${SELF_HOST_CALLS_ENABLED:-}" ]]; then
    inner="export SELF_HOST_CALLS_ENABLED=$(printf '%q' "$SELF_HOST_CALLS_ENABLED"); $inner"
  fi
  local stack_command
  printf -v stack_command '%q -lc %q' "$bash_bin" "$inner"

  log_info "Starting durable stack session: $session"
  tmux new-session -d -s "$session" "$stack_command"

  local elapsed=0
  local pane=""
  while (( elapsed < timeout_seconds )); do
    pane="$(tmux capture-pane -t "$session" -p -S -2000 2>/dev/null || true)"
    if [[ "$pane" == *"__UNITY_STACK_UP_EXIT_0__"* ]]; then
      log_success "Durable stack session is ready: $session"
      break
    fi
    if [[ "$pane" == *"__UNITY_STACK_UP_EXIT_"* && "$pane" != *"__UNITY_STACK_UP_EXIT_0__"* ]]; then
      log_error "Durable stack startup failed in tmux session: $session"
      echo "$pane"
      return 1
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  if (( elapsed >= timeout_seconds )); then
    log_error "Timed out waiting for durable stack startup"
    log_info "Attach for logs: tmux attach -t $session"
    return 1
  fi

  if ! curl -fsSI --max-time 10 "http://localhost:${console_port}/" >/dev/null; then
    log_error "Console did not respond at http://localhost:${console_port}"
    log_info "Attach for logs: tmux attach -t $session"
    return 1
  fi

  cmd_status
  echo ""
  log_success "Console is responding at http://localhost:${console_port}"
  log_info "Attach: tmux attach -t $session"
  log_info "Stop:   bash $SCRIPT_DIR/stack.sh down"
}

cmd_down() {
  local full_stop="false"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --full) full_stop="true"; shift ;;
      -h|--help)
        echo "Usage: unity stack down [--full]"
        echo ""
        echo "  Default: stop Console and stack ingress; keep Coordinator + Orchestra for scheduled tasks."
        echo "  --full:  stop everything, including background runtime."
        echo ""
        echo "  Also: unity service disable  (same as --full for background runtime)"
        return 0
        ;;
      *)
        log_error "Unknown option: $1"
        echo "Run: unity stack down --help"
        return 1
        ;;
    esac
  done

  if [[ ! -f "$CONSOLE_LOCAL_SCRIPT" ]]; then
    log_error "Missing $CONSOLE_LOCAL_SCRIPT"
    return 1
  fi

  export UNITY_HOME="${UNITY_HOME:-$HOME/.unity}"
  export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$UNITY_HOME}"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
  fi

  # Stopped with Console either way: the origin only serves frames for Console
  # pages, so leaving it listening would hold port 3100 for nothing.
  canvas_origin_stop

  if [[ "$full_stop" == "true" ]]; then
    if [[ -x "$SCRIPT_DIR/self_host_desktop.sh" ]]; then
      bash "$SCRIPT_DIR/self_host_desktop.sh" stop || true
    fi
    bash "$CONSOLE_LOCAL_SCRIPT" stop
    if [[ -x "$SCRIPT_DIR/service.sh" ]]; then
      bash "$SCRIPT_DIR/service.sh" stop || true
    fi
    log_success "Self-host stack and background runtime stopped"
    return 0
  fi

  if declare -F self_host_ensure_service_supervisor &>/dev/null \
    && [[ -f "$SCRIPT_DIR/service.sh" ]]; then
    self_host_ensure_service_supervisor "$SCRIPT_DIR/service.sh" || true
  fi

  if declare -F self_host_headless_scheduling_ready &>/dev/null \
    && self_host_headless_scheduling_ready; then
    SELF_HOST=1 bash "$CONSOLE_LOCAL_SCRIPT" stop --interactive-only
  else
    bash "$CONSOLE_LOCAL_SCRIPT" stop
    if declare -F self_host_stop_provider_trigger_worker &>/dev/null; then
      self_host_stop_provider_trigger_worker || true
    fi
  fi
  log_success "Self-host stack stopped"
}

health_check_line() {
  local label="$1"
  local url="$2"
  local expected="${3:-200}"
  local status=""
  status="$(curl -sS -o /dev/null -w '%{http_code}' -L --max-time 5 "$url" 2>/dev/null || true)"
  if [[ "$status" == "$expected" ]]; then
    printf '  %-12s ok (HTTP %s)\n' "$label" "$status"
  else
    printf '  %-12s not ready (HTTP %s)\n' "$label" "${status:-000}"
  fi
}

cmd_health_summary() {
  local console_port="${CONSOLE_PORT:-3000}"
  local orchestra_port="${ORCHESTRA_PORT:-8000}"
  local gateway_host="${UNITY_GATEWAY_HOST:-127.0.0.1}"
  local gateway_port="${UNITY_GATEWAY_PORT:-8001}"

  echo ""
  echo "Source Stack Health"
  echo "-------------------"
  health_check_line "Console" "http://127.0.0.1:${console_port}"
  health_check_line "Orchestra" "http://127.0.0.1:${orchestra_port}/v0/features"
  health_check_line "Gateway" "http://${gateway_host}:${gateway_port}/health"
  local runtime_file="${SELF_HOST_COORDINATOR_RUNTIME_FILE:-${UNITY_HOME:-$HOME/.unity}/coordinator-runtime.json}"
  if [[ ! -f "$runtime_file" ]]; then
    health_check_line "Login" "http://localhost:${console_port}/login"
    printf '  %-12s %s\n' "Coordinator" "pending signup"
  elif declare -F unity_cm_instance_count &>/dev/null; then
    health_check_line "Account" "http://localhost:${console_port}/account"
    printf '  %-12s %s instance(s)\n' "Coordinator" "$(unity_cm_instance_count)"
  fi
}

cmd_status() {
  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
  fi

  if [[ -f "$CONSOLE_LOCAL_SCRIPT" ]]; then
    bash "$CONSOLE_LOCAL_SCRIPT" status
  else
    log_error "Console local script not found"
    return 1
  fi

  if [[ -x "$SCRIPT_DIR/service.sh" ]]; then
    echo ""
    bash "$SCRIPT_DIR/service.sh" status
  fi

  echo ""
  canvas_origin_status_line

  cmd_health_summary

  if declare -F stack_state_print_console_env &>/dev/null; then
    echo ""
    echo "Expected Console env (non-secret)"
    echo "---------------------------------"
    stack_state_print_console_env 2>/dev/null || log_info "No source stack manifest found"
  fi
}

cmd_repair_console() {
  if declare -F stack_state_refuse_if_compose_active &>/dev/null; then
    stack_state_refuse_if_compose_active || return 1
  fi
  if [[ ! -f "$CONSOLE_LOCAL_SCRIPT" ]]; then
    log_error "Missing $CONSOLE_LOCAL_SCRIPT"
    return 1
  fi

  export SELF_HOST=1
  export DEPLOY_REPO_PATH
  export ORCHESTRA_REPO_PATH
  export UNITY_REPO_PATH
  export CONSOLE_REPO_PATH
  export ORCHESTRA_DB_PORT="$(default_orchestra_db_port)"
  export UNITY_HOME="${UNITY_HOME:-$HOME/.unity}"
  export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$UNITY_HOME}"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    export_self_host_coordinator_runtime_file
    load_self_host_repo_env_file "$UNITY_REPO_PATH/.env"
    if declare -F self_host_load_state_env_overlay &>/dev/null; then
      self_host_load_state_env_overlay
    fi
    if declare -F self_host_export_provider_trigger_env &>/dev/null; then
      self_host_export_provider_trigger_env
    fi
  fi

  setup_livekit_env || return 1

  if ! bash "$CONSOLE_LOCAL_SCRIPT" repair-console --self-host; then
    log_error "Console repair failed"
    return 1
  fi
  if declare -F stack_state_write_source &>/dev/null; then
    stack_state_write_source
  fi
  log_success "Console repaired at http://localhost:${CONSOLE_PORT:-3000}"
}

cmd_reset() {
  if [[ ! -f "$RESET_DB_SCRIPT" ]]; then
    log_error "Missing $RESET_DB_SCRIPT"
    return 1
  fi
  bash "$RESET_DB_SCRIPT" "$@"
}

cmd_dev_env() {
  if declare -F stack_state_print_console_env &>/dev/null; then
    stack_state_print_console_env
  else
    log_error "Missing stack_state.sh"
    return 1
  fi
}

cmd_smoke() {
  export SELF_HOST=1
  export ORCHESTRA_REPO_PATH
  export UNITY_REPO_PATH
  export CONSOLE_REPO_PATH
  export ORCHESTRA_DB_PORT="${ORCHESTRA_DB_PORT:-55432}"
  export UNITY_HOME="${UNITY_HOME:-$HOME/.unity}"
  export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$UNITY_HOME}"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    export_self_host_coordinator_runtime_file
  fi

  local py="$UNITY_REPO_PATH/.venv/bin/python"
  if [[ ! -x "$py" ]]; then
    py="python3"
  fi

  CONSOLE_PORT="${CONSOLE_PORT:-3000}" \
    ORCHESTRA_PORT="${ORCHESTRA_PORT:-8000}" \
    UNITY_GATEWAY_HOST="${UNITY_GATEWAY_HOST:-127.0.0.1}" \
    UNITY_GATEWAY_PORT="${UNITY_GATEWAY_PORT:-8001}" \
    SELF_HOST_COORDINATOR_RUNTIME_FILE="${SELF_HOST_COORDINATOR_RUNTIME_FILE:-}" \
    STACK_SCRIPT="$SCRIPT_DIR/stack.sh" \
    "$py" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def log(kind: str, message: str) -> None:
    print(f"[{kind}] {message}")


def request_status(method: str, url: str, *, headers=None, body=None, timeout=30):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 - smoke output should report any transport failure.
        return 0, f"{type(exc).__name__}: {exc}"


failures: list[str] = []
recovery_cmd = f"bash {os.environ['STACK_SCRIPT']} redeploy"


def check(
    name: str,
    method: str,
    url: str,
    expected: set[int],
    *,
    headers=None,
    body=None,
    recovery: str | None = None,
) -> None:
    status, text = request_status(method, url, headers=headers, body=body)
    if status in expected:
        log("OK", f"{name}: HTTP {status}")
        return
    preview = text.replace("\n", " ")[:300]
    failures.append(name)
    log("ERROR", f"{name}: expected {sorted(expected)}, got HTTP {status}. {preview}")
    if recovery:
        log("INFO", f"{name} recovery: {recovery}")


console = f"http://127.0.0.1:{os.environ['CONSOLE_PORT']}"
orchestra = f"http://127.0.0.1:{os.environ['ORCHESTRA_PORT']}"
gateway = f"http://{os.environ['UNITY_GATEWAY_HOST']}:{os.environ['UNITY_GATEWAY_PORT']}"

check("Console", "GET", console, {200})
check("Orchestra features", "GET", f"{orchestra}/v0/features", {200})
check(
    "Unity gateway",
    "GET",
    f"{gateway}/health",
    {200},
    recovery=f"{recovery_cmd}  # restarts the service gateway",
)
check(
    "Registration path",
    "POST",
    f"{console}/api/auth/email/register",
    {422},
    body={
        "email": "unity-smoke@example.local",
        "name": "Smoke",
        "lastName": "Check",
        "password": "Aa1!TemporaryLocalSmokePassword12345",  # pragma: allowlist secret
    },
)

runtime_file = os.environ.get("SELF_HOST_COORDINATOR_RUNTIME_FILE")
credentials: dict = {}
if runtime_file:
    try:
        credentials = json.loads(Path(runtime_file).read_text(encoding="utf-8"))
    except FileNotFoundError:
        log("INFO", f"Coordinator checks skipped: register in Console to create {runtime_file}.")
    except json.JSONDecodeError as exc:
        failures.append("Coordinator runtime file")
        log("ERROR", f"Coordinator runtime file is invalid JSON: {exc}")
        log("INFO", f"Coordinator runtime file recovery: {recovery_cmd}")

api_key = credentials.get("apiKey") or credentials.get("api_key")
assistant_id = credentials.get("coordinatorAgentId") or credentials.get("coordinator_agent_id")
if api_key and assistant_id:
    auth_headers = {"apiKey": str(api_key)}
    catalog_query = urllib.parse.urlencode(
        {
            "projectName": "Builtins",
            "context": "Integrations/Apps",
            "limit": "1",
            "offset": "0",
            "fromFields": "canonical_app_slug,display_name,source_type",
            "sorting": json.dumps({"display_name": "ascending"}),
        }
    )
    check(
        "Integration catalog",
        "GET",
        f"{console}/api/logs?{catalog_query}",
        {200},
        headers=auth_headers,
        recovery=f"{recovery_cmd}  # resets credentials and seeds Builtins",
    )
    check(
        "Assistant presence wake",
        "POST",
        f"{console}/api/assistant/{assistant_id}/system-event",
        {202},
        headers=auth_headers,
        body={
            "eventType": "assistant_presence_observed",
            "message": "Self-host smoke check.",
            "extraEventFields": {
                "source": "assistant_profile",
                "reason": "selection",
            },
        },
        recovery=f"{recovery_cmd}  # refreshes Coordinator credentials and runtime",
    )
else:
    log("INFO", "Coordinator checks skipped: register in Console to create the local owner.")

if failures:
    log("ERROR", "Self-host smoke failed: " + ", ".join(failures))
    sys.exit(1)

log("OK", "Self-host smoke passed")
PY
}

cmd_logs() {
  if [[ ! -f "$CONSOLE_LOCAL_SCRIPT" ]]; then
    log_error "Missing $CONSOLE_LOCAL_SCRIPT"
    return 1
  fi
  # The canvas origin is the one service this script starts itself, so its log is
  # the one it has to serve; everything else belongs to Console's local.sh.
  if [[ "${1:-}" == "canvas" ]]; then
    if [[ ! -f "$CANVAS_ORIGIN_LOG_FILE" ]]; then
      log_error "No canvas origin log at $CANVAS_ORIGIN_LOG_FILE"
      return 1
    fi
    tail -f "$CANVAS_ORIGIN_LOG_FILE"
    return 0
  fi
  # Console's local.sh owns the per-service logfiles for the stack
  # (console|orchestra|pubsub|stripe); delegate so there's one log surface.
  bash "$CONSOLE_LOCAL_SCRIPT" logs "$@"
}

main() {
  local cmd="${1:-up}"
  shift || true
  case "$cmd" in
    up)
      if [[ "${1:-}" == "--durable" ]]; then
        shift
        cmd_up_durable "$@"
      else
        cmd_redeploy "$@"
      fi
      ;;
    redeploy|fresh) cmd_redeploy "$@" ;;
    resume) cmd_resume "$@" ;;
    down|stop) cmd_down "$@" ;;
    status) cmd_status "$@" ;;
    logs) cmd_logs "$@" ;;
    smoke) cmd_smoke "$@" ;;
    repair-console|restart-console) cmd_repair_console "$@" ;;
    reset|reset-db) cmd_reset "$@" ;;
    seed-builtins) cmd_seed_builtins "$@" ;;
    dev-env|print-console-env) cmd_dev_env "$@" ;;
    sync-comms) cmd_sync_comms "$@" ;;
    doctor|check) cmd_doctor "$@" ;;
    help|-h|--help)
      sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'
      ;;
    *)
      log_error "Unknown command: $cmd"
      echo "Run: $0 help"
      return 1
      ;;
  esac
}

main "$@"
