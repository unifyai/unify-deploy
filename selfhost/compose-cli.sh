#!/usr/bin/env bash
# =============================================================================
# compose-cli.sh — Docker Compose lifecycle for Droid self-host
# =============================================================================
set -euo pipefail

DROID_HOME="${DROID_HOME:-$HOME/.droid}"
COMPOSE_DIR="${DROID_COMPOSE_DIR:-$DROID_HOME}"
COMPOSE_FILE="${COMPOSE_FILE:-$COMPOSE_DIR/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-$COMPOSE_DIR/.env}"

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

# Compose gives the caller's shell environment precedence over --env-file
# values during ${VAR} interpolation, so stray exports (direnv, dotfiles, CI)
# would silently override the stack's secrets. Run compose under a minimal
# environment so $ENV_FILE is the single source of truth; keep only PATH,
# HOME (docker CLI config), TERM, and Docker connectivity settings.
compose() {
  env -i \
    PATH="$PATH" \
    HOME="$HOME" \
    TERM="${TERM:-}" \
    ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} \
    ${DOCKER_CONFIG:+DOCKER_CONFIG="$DOCKER_CONFIG"} \
    ${DOCKER_CONTEXT:+DOCKER_CONTEXT="$DOCKER_CONTEXT"} \
    ${DOCKER_CERT_PATH:+DOCKER_CERT_PATH="$DOCKER_CERT_PATH"} \
    ${DOCKER_TLS_VERIFY:+DOCKER_TLS_VERIFY="$DOCKER_TLS_VERIFY"} \
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
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

cmd_up() {
  require_compose
  mkdir -p "$(grep -E '^DROID_WORKSPACE_HOST=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | sed "s/^\\${HOME}/$HOME/" || echo "$HOME/Droid/Local")"
  log_info "Starting Droid self-host stack..."
  compose up -d "$@"
  if [[ $# -eq 0 ]]; then
    if _has_env COMPOSIO_API_KEY; then
      log_info "Builtins catalogue seed runs in the background (~30 min with Composio); Console is ready now"
    else
      log_info "Builtins catalogue seed runs in the background (usually a few minutes)"
    fi
    log_info "Watch progress: droid stack logs droid-builtins-seed"
  fi
  log_ok "Stack is up — open ${NEXTAUTH_URL:-http://127.0.0.1:3000}"
}

cmd_integrations_sync() {
  require_compose
  if ! _has_env COMPOSIO_API_KEY; then
    log_info "COMPOSIO_API_KEY not set — skipping integrations catalog sync"
    return 0
  fi
  log_info "Composio integration catalogue sync runs via droid-builtins-seed"
  cmd_builtins_sync
}

cmd_builtins_sync() {
  require_compose
  log_info "Starting Builtins catalogue seed in the background..."
  compose up -d --force-recreate droid-builtins-seed
  if _has_env COMPOSIO_API_KEY; then
    log_info "Composio configured — full catalogue may take ~30 minutes"
  fi
  log_info "Watch progress: droid stack logs droid-builtins-seed"
}

cmd_down() {
  require_compose
  if [[ "${1:-}" == "--full" ]]; then
    log_info "Stopping all services..."
    compose down
  else
    log_info "Stopping Console UI (runtime services keep running)..."
    compose stop console
    log_ok "Console stopped. CM, gateway, and scheduler remain active."
    log_info "Stop everything: droid stack down --full"
  fi
}

cmd_restart() {
  require_compose
  log_info "Recreating stack with updated .env..."
  compose up -d --force-recreate
  log_info "Builtins catalogue seed runs in the background — droid stack logs droid-builtins-seed"
  log_ok "Restart complete"
}

cmd_status() {
  require_compose
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
  echo -e "${BOLD}Self-host compose doctor${NC}"
  echo "========================"
  if docker info >/dev/null 2>&1; then
    log_ok "Docker daemon running"
  else
    log_err "Docker daemon not running"
  fi
  if [[ -f "$ENV_FILE" ]]; then
    log_ok ".env present"
    for key in ORCHESTRA_ADMIN_KEY NEXTAUTH_SECRET JWT_SECRET POSTGRES_PASSWORD \
      INTEGRATION_CONFIRMATION_SECRET; do
      if _has_env "$key"; then
        log_ok "Secret configured ($key)"
      else
        log_err "Missing installer secret: $key — re-run install or set manually"
      fi
    done
    if _has_env OPENAI_API_KEY || _has_env ANTHROPIC_API_KEY || _has_env DEEPSEEK_API_KEY; then
      log_ok "LLM provider key configured"
    else
      log_err "Missing LLM key — set OPENAI_API_KEY, ANTHROPIC_API_KEY, or DEEPSEEK_API_KEY"
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
      log_ok "Composio API key configured (integration catalogue seeds via droid-builtins-seed)"
    else
      log_info "COMPOSIO_API_KEY not set — third-party app integrations disabled (optional)"
    fi
  else
    log_err ".env missing at $ENV_FILE"
  fi
  local seed_status
  seed_status="$(compose ps -a --format '{{.Service}}\t{{.State}}\t{{.ExitCode}}' 2>/dev/null \
    | awk '$1=="orchestra-seed"{print $2"\t"$3; exit}')"
  if [[ "$seed_status" == "exited	0" ]]; then
    log_ok "Orchestra billing seed completed"
  elif [[ -n "$seed_status" ]]; then
    log_warn "orchestra-seed status: ${seed_status//$'\t'/ }"
  else
    log_warn "orchestra-seed not found — run: droid stack up"
  fi
  local builtins_status
  builtins_status="$(compose ps -a --format '{{.Service}}\t{{.State}}\t{{.ExitCode}}' 2>/dev/null \
    | awk '$1=="droid-builtins-seed"{print $2"\t"$3; exit}')"
  case "$builtins_status" in
    running*)
      log_info "Builtins catalogue seed in progress — droid stack logs droid-builtins-seed"
      ;;
    "exited	0")
      log_ok "Builtins catalogue seed completed"
      ;;
    exited*)
      log_warn "Builtins catalogue seed failed (${builtins_status//$'\t'/ }) — run: droid stack builtins-sync"
      ;;
    *)
      log_info "Builtins catalogue seed not started — run: droid stack builtins-sync"
      ;;
  esac
  compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
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
