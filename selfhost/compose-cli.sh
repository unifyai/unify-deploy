#!/usr/bin/env bash
# =============================================================================
# compose-cli.sh — Docker Compose lifecycle for Unity self-host
# =============================================================================
set -euo pipefail

UNITY_HOME="${UNITY_HOME:-$HOME/.unity}"
COMPOSE_DIR="${UNITY_COMPOSE_DIR:-$UNITY_HOME}"
COMPOSE_FILE="${COMPOSE_FILE:-$COMPOSE_DIR/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-$COMPOSE_DIR/.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
STACK_STATE_SCRIPT="$SCRIPT_DIR/stack_state.sh"

if [[ -f "$STACK_STATE_SCRIPT" ]]; then
  # shellcheck disable=SC1090
  source "$STACK_STATE_SCRIPT"
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info() { echo -e "${CYAN}→${NC} $1"; }
log_ok() { echo -e "${GREEN}✓${NC} $1"; }
log_warn() { echo -e "${YELLOW}⚠${NC} $1"; }
log_err() { echo -e "${RED}✗${NC} $1" >&2; }

_env_value() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null \
    | tail -1 \
    | cut -d= -f2- \
    | tr -d '\r'
}

_enabled() {
  case "$(_env_value "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

compose_profile_args() {
  if _enabled SELF_HOST_INTERNAL_COMMS_ENABLED \
    || _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    printf '%s\n' --profile internal-comms
  fi
  if _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    printf '%s\n' --profile internal-calls
  fi
  if _enabled SELF_HOST_PROVIDER_TRIGGERS_ENABLED; then
    printf '%s\n' --profile provider-triggers
  fi
}

# Compose gives the caller's shell environment precedence over --env-file
# values during ${VAR} interpolation, so stray exports (direnv, dotfiles, CI)
# would silently override the stack's secrets. Run compose under a minimal
# environment so $ENV_FILE is the single source of truth; keep only PATH,
# HOME (docker CLI config), TERM, and Docker connectivity settings.
compose() {
  local profile_args=()
  while IFS= read -r argument; do
    [[ -n "$argument" ]] && profile_args+=("$argument")
  done < <(compose_profile_args)
  env -i \
    PATH="$PATH" \
    HOME="$HOME" \
    TERM="${TERM:-}" \
    ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} \
    ${DOCKER_CONFIG:+DOCKER_CONFIG="$DOCKER_CONFIG"} \
    ${DOCKER_CONTEXT:+DOCKER_CONTEXT="$DOCKER_CONTEXT"} \
    ${DOCKER_CERT_PATH:+DOCKER_CERT_PATH="$DOCKER_CERT_PATH"} \
    ${DOCKER_TLS_VERIFY:+DOCKER_TLS_VERIFY="$DOCKER_TLS_VERIFY"} \
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
      "${profile_args[@]}" "$@"
}

require_compose() {
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    log_err "Compose stack not found at $COMPOSE_FILE"
    log_info "Run the self-host installer: unity-deploy/selfhost/install-compose.sh"
    exit 1
  fi
  if ! command -v docker >/dev/null 2>&1; then
    log_err "Docker is required"
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    log_err "Docker daemon is not running"
    exit 1
  fi
}

validate_optional_profiles() {
  if _enabled SELF_HOST_PROVIDER_TRIGGERS_ENABLED; then
    if ! _has_env TRIGGER_EVENT_WRAPPING_MASTER_KEY; then
      log_err "SELF_HOST_PROVIDER_TRIGGERS_ENABLED requires TRIGGER_EVENT_WRAPPING_MASTER_KEY"
      return 1
    fi
    if ! _has_env ORCHESTRA_TRIGGER_CALLBACK_BASE_URL; then
      log_err "SELF_HOST_PROVIDER_TRIGGERS_ENABLED requires ORCHESTRA_TRIGGER_CALLBACK_BASE_URL (public HTTPS)"
      return 1
    fi
    case "$(_env_value ORCHESTRA_TRIGGER_CALLBACK_BASE_URL)" in
      https://*) ;;
      *)
        log_err "ORCHESTRA_TRIGGER_CALLBACK_BASE_URL must start with https://"
        return 1
        ;;
    esac
  fi

  if ! _enabled SELF_HOST_INTERNAL_COMMS_ENABLED \
    && ! _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    return 0
  fi

  local twilio_file="$COMPOSE_DIR/comms_twilio.env"
  local gmail_file="$COMPOSE_DIR/comms_sa.json"
  local main_sid_configured=false
  local main_token_configured=false
  local main_twilio_configured=false
  local wa_sid_configured=false
  local wa_token_configured=false
  local wa_twilio_configured=false
  local gmail_configured=false
  if [[ -s "$twilio_file" ]] \
    && grep -qE '^TWILIO_ACCOUNT_SID=.+$' "$twilio_file"; then
    main_sid_configured=true
  fi
  if [[ -s "$twilio_file" ]] \
    && grep -qE '^TWILIO_AUTH_TOKEN=.+$' "$twilio_file"; then
    main_token_configured=true
  fi
  if [[ -s "$twilio_file" ]] \
    && grep -qE '^TWILIO_WA_ACCOUNT_SID=.+$' "$twilio_file"; then
    wa_sid_configured=true
  fi
  if [[ -s "$twilio_file" ]] \
    && grep -qE '^TWILIO_WA_AUTH_TOKEN=.+$' "$twilio_file"; then
    wa_token_configured=true
  fi
  if [[ "$main_sid_configured" == "true" && "$main_token_configured" == "true" ]]; then
    main_twilio_configured=true
  fi
  if [[ "$wa_sid_configured" == "true" && "$wa_token_configured" == "true" ]]; then
    wa_twilio_configured=true
  fi
  if [[ "$main_sid_configured" != "$main_token_configured" ]]; then
    log_err "Twilio main account SID/token must be configured as a complete pair"
    return 1
  fi
  if [[ "$wa_sid_configured" != "$wa_token_configured" ]]; then
    log_err "Twilio WhatsApp SID/token must be configured as a complete pair"
    return 1
  fi
  if [[ -s "$gmail_file" ]] \
    && grep -q '"client_email"[[:space:]]*:' "$gmail_file" \
    && grep -q '"private_key"[[:space:]]*:' "$gmail_file"; then
    gmail_configured=true
  fi
  if _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    if [[ "$main_twilio_configured" != "true" ]]; then
      log_err "Internal calls need main-account Twilio credentials in $twilio_file"
      return 1
    fi
    if [[ "$wa_twilio_configured" != "true" ]]; then
      log_err "Internal calls need WhatsApp Twilio credentials in $twilio_file"
      return 1
    fi
    local key
    for key in LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET LIVEKIT_SIP_URI; do
      if ! _has_env "$key"; then
        log_err "Internal calls require $key in $ENV_FILE"
        return 1
      fi
    done
  else
    if [[ "$gmail_configured" != "true" ]]; then
      log_err "Internal comms without calls requires Gmail credentials at $gmail_file"
      return 1
    fi
  fi
}

release_owned_webhooks() {
  if ! _enabled SELF_HOST_INTERNAL_COMMS_ENABLED \
    && ! _enabled SELF_HOST_INTERNAL_CALLS_ENABLED \
    && ! compose ps -a --services 2>/dev/null \
      | awk '$0=="comms-bridge" || $0=="call-controller"{found=1} END{exit !found}'; then
      return 0
  fi
  log_info "Releasing this installation's communications callbacks..."
  compose run --rm --no-deps call-controller --release
}

stop_disabled_profile_services() {
  if ! _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    if compose ps -a --services 2>/dev/null \
      | awk '$0=="call-controller"{found=1} END{exit !found}'; then
      if ! compose run --rm --no-deps call-controller --release-voice; then
        log_err "Refusing to disable internal calls because voice release failed"
        return 1
      fi
    fi
    compose stop call-controller call-tunnel call-proxy >/dev/null 2>&1 || true
    compose rm -f call-controller call-tunnel call-proxy >/dev/null 2>&1 || true
  fi
  if ! _enabled SELF_HOST_INTERNAL_COMMS_ENABLED \
    && ! _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    if compose ps -a --services 2>/dev/null \
      | awk '$0=="comms-bridge"{found=1} END{exit !found}'; then
      if ! compose run --rm --no-deps call-controller --release-text; then
        log_err "Refusing to disable internal comms because callback release failed"
        return 1
      fi
    fi
    compose stop comms-bridge >/dev/null 2>&1 || true
    compose rm -f comms-bridge comms-runtime-init >/dev/null 2>&1 || true
  fi
  if ! _enabled SELF_HOST_PROVIDER_TRIGGERS_ENABLED; then
    compose stop orchestra-trigger-worker trigger-ingress >/dev/null 2>&1 || true
    compose rm -f orchestra-trigger-worker trigger-ingress >/dev/null 2>&1 || true
  fi
}

rollback_failed_start() {
  log_warn "Rolling back failed stack startup..."
  if ! release_owned_webhooks; then
    log_err "Webhook release failed; leaving the call edge running to avoid dead callbacks"
    return 1
  fi
  compose down || true
}

verify_optional_profiles() {
  local container_id health attempt
  if _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    container_id="$(compose ps -q call-controller)"
    [[ -n "$container_id" ]] || {
      log_err "Call controller was not created"
      return 1
    }
    for attempt in $(seq 1 60); do
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      case "$health" in
        healthy) log_ok "Internal call edge acquired"; break ;;
        exited|dead|unhealthy)
          log_err "Call controller failed before acquiring the call edge"
          return 1
          ;;
      esac
      sleep 2
    done
    if [[ "$health" != "healthy" ]]; then
      log_err "Timed out waiting for the internal call edge"
      return 1
    fi
  fi
  if _enabled SELF_HOST_INTERNAL_COMMS_ENABLED \
    || _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    container_id="$(compose ps -q comms-bridge)"
    [[ -n "$container_id" ]] || {
      log_err "Internal communications bridge was not created"
      return 1
    }
    health=""
    for attempt in $(seq 1 60); do
      health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      case "$health" in
        healthy) log_ok "Internal communications bridge acquired"; return 0 ;;
        exited|dead|unhealthy)
          log_err "Internal communications bridge failed before acquiring callbacks"
          return 1
          ;;
      esac
      sleep 2
    done
    log_err "Timed out waiting for the internal communications bridge"
    return 1
  fi
}

cmd_up() {
  require_compose
  if declare -F stack_state_refuse_if_source_active >/dev/null 2>&1; then
    stack_state_refuse_if_source_active || return 1
  fi
  validate_optional_profiles || return 1
  stop_disabled_profile_services || return 1
  mkdir -p "$(grep -E '^UNITY_WORKSPACE_HOST=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | sed "s/^\\${HOME}/$HOME/" || echo "$HOME/Unity/Local")"
  log_info "Starting Unity self-host stack..."
  if ! compose up -d "$@"; then
    rollback_failed_start
    return 1
  fi
  if ! verify_optional_profiles; then
    rollback_failed_start
    return 1
  fi
  if [[ $# -eq 0 ]]; then
    if _has_env COMPOSIO_API_KEY; then
      log_info "Builtins catalogue seed runs in the background (~30 min with Composio); Console is ready now"
    else
      log_info "Builtins catalogue seed runs in the background (usually a few minutes)"
    fi
    log_info "Watch progress: unity stack logs unity-builtins-seed"
  fi
  log_ok "Stack is up — open ${NEXTAUTH_URL:-http://127.0.0.1:3000}"
}

cmd_integrations_sync() {
  require_compose
  if ! _has_env COMPOSIO_API_KEY; then
    log_info "COMPOSIO_API_KEY not set — skipping integrations catalog sync"
    return 0
  fi
  log_info "Composio integration catalogue sync runs via unity-builtins-seed"
  cmd_builtins_sync
}

cmd_builtins_sync() {
  require_compose
  log_info "Starting Builtins catalogue seed in the background..."
  compose up -d --force-recreate unity-builtins-seed
  if _has_env COMPOSIO_API_KEY; then
    log_info "Composio configured — full catalogue may take ~30 minutes"
  fi
  log_info "Watch progress: unity stack logs unity-builtins-seed"
}

cmd_down() {
  require_compose
  if [[ "${1:-}" == "--full" ]]; then
    if ! release_owned_webhooks; then
      log_err "Refusing full shutdown because callback ownership release failed"
      return 1
    fi
    log_info "Stopping all services..."
    compose down
  else
    log_info "Stopping Console UI (runtime services keep running)..."
    compose stop console
    log_ok "Console stopped. CM, gateway, and scheduler remain active."
    log_info "Stop everything: unity stack down --full"
  fi
}

cmd_restart() {
  require_compose
  validate_optional_profiles || return 1
  if ! release_owned_webhooks; then
    log_err "Refusing restart because callback ownership release failed"
    return 1
  fi
  stop_disabled_profile_services || return 1
  log_info "Recreating stack with updated .env..."
  if ! compose up -d --force-recreate; then
    rollback_failed_start
    return 1
  fi
  if ! verify_optional_profiles; then
    rollback_failed_start
    return 1
  fi
  log_info "Builtins catalogue seed runs in the background — unity stack logs unity-builtins-seed"
  log_ok "Restart complete"
}

cmd_status() {
  require_compose
  if _enabled SELF_HOST_INTERNAL_COMMS_ENABLED \
    || _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    log_info "Internal communications profile enabled"
  else
    log_info "Internal communications profiles disabled"
  fi
  compose ps
}

cmd_logs() {
  require_compose
  compose logs -f "${@:-}"
}

_has_env() {
  local key="$1"
  grep -qE "^${key}=.+$" "$ENV_FILE" 2>/dev/null
}

cmd_doctor() {
  require_compose
  local doctor_ok=true
  echo -e "${BOLD}Self-host compose doctor${NC}"
  echo "========================"
  if docker info >/dev/null 2>&1; then
    log_ok "Docker daemon running"
  else
    log_err "Docker daemon not running"
    doctor_ok=false
  fi
  if [[ -f "$ENV_FILE" ]]; then
    log_ok ".env present"
    for key in ORCHESTRA_ADMIN_KEY NEXTAUTH_SECRET JWT_SECRET POSTGRES_PASSWORD \
      INTEGRATION_CONFIRMATION_SECRET; do
      if _has_env "$key"; then
        log_ok "Secret configured ($key)"
      else
        log_err "Missing installer secret: $key — re-run install or set manually"
        doctor_ok=false
      fi
    done
    if _has_env OPENAI_API_KEY || _has_env ANTHROPIC_API_KEY || _has_env DEEPSEEK_API_KEY; then
      log_ok "LLM provider key configured"
    else
      log_err "Missing LLM key — set OPENAI_API_KEY, ANTHROPIC_API_KEY, or DEEPSEEK_API_KEY"
      doctor_ok=false
    fi
    if _has_env OPENAI_API_KEY; then
      log_ok "OpenAI key present (chat and tool-search embeddings)"
    elif _has_env ANTHROPIC_API_KEY || _has_env DEEPSEEK_API_KEY; then
      log_warn "No OPENAI_API_KEY — tool-search embeddings need OpenAI"
    fi
    if _has_env DEEPGRAM_API_KEY && { _has_env CARTESIA_API_KEY || _has_env ELEVEN_API_KEY; }; then
      log_ok "Voice BYOK keys configured"
    else
      log_warn "Voice calls need DEEPGRAM_API_KEY and a TTS key (CARTESIA_API_KEY or ELEVEN_API_KEY)"
    fi
    if _has_env COMPOSIO_API_KEY; then
      log_ok "Composio API key configured (integration catalogue seeds via unity-builtins-seed)"
    else
      log_info "COMPOSIO_API_KEY not set — third-party app integrations disabled (optional)"
    fi
    if _enabled SELF_HOST_INTERNAL_COMMS_ENABLED \
      || _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
      if validate_optional_profiles; then
        log_ok "Internal communications credentials configured"
      else
        log_err "Internal communications profile configuration invalid"
        doctor_ok=false
      fi
    else
      log_info "Internal communications profiles disabled (default)"
    fi
    if _enabled SELF_HOST_PROVIDER_TRIGGERS_ENABLED; then
      if validate_optional_profiles; then
        log_ok "Provider-trigger profile prerequisites configured"
      else
        log_err "Provider-trigger profile configuration invalid"
        doctor_ok=false
      fi
      local worker_id worker_health ingress_id
      worker_id="$(compose ps -q orchestra-trigger-worker 2>/dev/null || true)"
      worker_health=""
      if [[ -n "$worker_id" ]]; then
        worker_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$worker_id" 2>/dev/null || true)"
      fi
      if [[ "$worker_health" == "healthy" ]]; then
        log_ok "Provider-trigger worker healthy"
      else
        log_warn "Provider-trigger worker is not healthy (${worker_health:-not started})"
      fi
      ingress_id="$(compose ps -q trigger-ingress 2>/dev/null || true)"
      if [[ -n "$ingress_id" ]]; then
        log_ok "Provider-trigger ingress running (local test URL: http://127.0.0.1:8088)"
      else
        log_warn "Provider-trigger ingress not started"
      fi
    else
      log_info "Provider-trigger profile disabled (default)"
    fi
  else
    log_err ".env missing at $ENV_FILE"
    doctor_ok=false
  fi
  local seed_status
  seed_status="$(compose ps -a --format '{{.Service}}\t{{.State}}\t{{.ExitCode}}' 2>/dev/null \
    | awk '$1=="orchestra-seed"{print $2"\t"$3; exit}')"
  if [[ "$seed_status" == "exited	0" ]]; then
    log_ok "Orchestra billing seed completed"
  elif [[ -n "$seed_status" ]]; then
    log_warn "orchestra-seed status: ${seed_status//$'\t'/ }"
  else
    log_warn "orchestra-seed not found — run: unity stack up"
  fi
  local builtins_status
  builtins_status="$(compose ps -a --format '{{.Service}}\t{{.State}}\t{{.ExitCode}}' 2>/dev/null \
    | awk '$1=="unity-builtins-seed"{print $2"\t"$3; exit}')"
  case "$builtins_status" in
    running*)
      log_info "Builtins catalogue seed in progress — unity stack logs unity-builtins-seed"
      ;;
    "exited	0")
      log_ok "Builtins catalogue seed completed"
      ;;
    exited*)
      log_warn "Builtins catalogue seed failed (${builtins_status//$'\t'/ }) — run: unity stack builtins-sync"
      ;;
    *)
      log_info "Builtins catalogue seed not started — run: unity stack builtins-sync"
      ;;
  esac
  if _enabled SELF_HOST_INTERNAL_COMMS_ENABLED \
    || _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    local bridge_id bridge_health
    bridge_id="$(compose ps -q comms-bridge)"
    bridge_health=""
    if [[ -n "$bridge_id" ]]; then
      bridge_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$bridge_id" 2>/dev/null || true)"
    fi
    if [[ "$bridge_health" == "healthy" ]]; then
      log_ok "Internal communications bridge healthy"
    else
      log_warn "Internal communications bridge is not healthy (${bridge_health:-not started})"
    fi
  fi
  if _enabled SELF_HOST_INTERNAL_CALLS_ENABLED; then
    local controller_id controller_health
    controller_id="$(compose ps -q call-controller)"
    controller_health=""
    if [[ -n "$controller_id" ]]; then
      controller_health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$controller_id" 2>/dev/null || true)"
    fi
    if [[ "$controller_health" == "healthy" ]]; then
      log_ok "Internal call edge healthy"
    else
      log_warn "Internal call edge is not healthy (${controller_health:-not started})"
    fi
  fi
  compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
  [[ "$doctor_ok" == "true" ]]
}

cmd_pull() {
  require_compose
  compose pull
}

main() {
  local sub="${1:-up}"
  shift || true
  case "$sub" in
    up) cmd_up "$@" ;;
    down|stop) cmd_down "$@" ;;
    restart) cmd_restart "$@" ;;
    status|ps) cmd_status "$@" ;;
    logs) cmd_logs "$@" ;;
    doctor) cmd_doctor "$@" ;;
    pull) cmd_pull "$@" ;;
    integrations-sync) cmd_integrations_sync "$@" ;;
    builtins-sync) cmd_builtins_sync "$@" ;;
    *)
      echo "Usage: compose-cli.sh {up|down|restart|status|logs|doctor|pull|integrations-sync|builtins-sync}" >&2
      exit 1
      ;;
  esac
}

main "$@"
