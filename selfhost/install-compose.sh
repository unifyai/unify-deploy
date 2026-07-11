#!/usr/bin/env bash
# =============================================================================
# install-compose.sh — Stranger install via prebuilt Docker Compose images
# =============================================================================
set -euo pipefail
umask 077

# This installer lives in unity-deploy/selfhost/. Run from a unity-deploy
# checkout it copies the local compose bundle; the remote fallback targets the
# (private) unity-deploy repo and requires credentials.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
BRANCH="${BRANCH:-staging}"
REPO_RAW="https://raw.githubusercontent.com/unifyai/unity-deploy/${BRANCH}"
SELFHOST_SRC="${INSTALL_SELFHOST_SRC:-${SCRIPT_DIR:+$SCRIPT_DIR/../deploy/selfhost}}"

UNITY_HOME="${UNITY_HOME:-$HOME/.unity}"
CLI_DIR="${CLI_DIR:-$HOME/.local/bin}"
CREATE_CLI=true
NON_INTERACTIVE="${NON_INTERACTIVE:-false}"
INSTALLED_COMPOSE_CLI=""

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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) UNITY_HOME="$2"; shift 2 ;;
    --no-cli) CREATE_CLI=false; shift ;;
    --non-interactive) NON_INTERACTIVE=true; shift ;;
    -h|--help)
      cat <<EOF
Unity compose installer — requires Docker only.

  curl -fsSL .../install.sh | bash

Options:
  --dir PATH            Install directory (default: ~/.unity)
  --no-cli              Skip unify CLI shim
  --non-interactive     Import keys from environment; skip prompts
EOF
      exit 0
      ;;
    *) log_err "Unknown option: $1"; exit 1 ;;
  esac
done

print_banner() {
  echo ""
  echo -e "${BOLD}Unity Self-Host (Docker Compose)${NC}"
  echo "Pull prebuilt images, configure BYOK keys, and open Console."
  echo ""
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    log_err "Docker is required."
    case "$(uname -s)" in
      Darwin) log_info "Install Docker Desktop: https://www.docker.com/products/docker-desktop/" ;;
      *) log_info "Install Docker: https://docs.docker.com/get-docker/" ;;
    esac
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    log_err "Docker daemon is not running."
    exit 1
  fi
  log_ok "Docker ready"
}

fetch_remote() {
  local rel="$1"
  local dest="$2"
  curl -fsSL "${REPO_RAW}/${rel}" -o "$dest"
}

install_compose_bundle() {
  mkdir -p -m 0700 "$UNITY_HOME"
  chmod 0700 "$UNITY_HOME"
  mkdir -p "${UNITY_HOME}/workspace"
  local files=(
    docker-compose.yml
    Caddyfile
    call-proxy.Caddyfile
    coordinator.env
    .env.example
    ensure-pubsub-topics.sh
    cm-entrypoint.sh
    gateway-entrypoint.sh
    desktop-entrypoint.sh
    publish-desktop-ready.sh
    comms-bridge-entrypoint.sh
    call-controller-entrypoint.sh
    load-comms-secrets.sh
    comms_ingress_bridge.py
    provision_call_sip.py
    sync_comms_webhooks.py
    integration-bootstrap.selfhost.toml
    README.md
  )
  local seen="|" file
  for file in "${files[@]}"; do
    if [[ "$seen" == *"|${file}|"* ]]; then
      log_err "Compose bundle collision: $file appears more than once"
      return 1
    fi
    seen+="${file}|"
    if [[ -e "$UNITY_HOME/$file" && ! -f "$UNITY_HOME/$file" ]]; then
      log_err "Refusing to replace non-file bundle path: $UNITY_HOME/$file"
      return 1
    fi
    if [[ -n "$SELFHOST_SRC" && -f "$SELFHOST_SRC/$file" ]]; then
      cp "$SELFHOST_SRC/$file" "$UNITY_HOME/$file"
    else
      fetch_remote "deploy/selfhost/$file" "$UNITY_HOME/$file"
    fi
  done
  chmod 0644 \
    "$UNITY_HOME/docker-compose.yml" \
    "$UNITY_HOME/Caddyfile" \
    "$UNITY_HOME/call-proxy.Caddyfile" \
    "$UNITY_HOME/.env.example" \
    "$UNITY_HOME/comms_ingress_bridge.py" \
    "$UNITY_HOME/provision_call_sip.py" \
    "$UNITY_HOME/sync_comms_webhooks.py" \
    "$UNITY_HOME/integration-bootstrap.selfhost.toml" \
    "$UNITY_HOME/README.md"
  chmod 0600 "$UNITY_HOME/coordinator.env"
  chmod 0755 "$UNITY_HOME"/*.sh
  if [[ ! -f "$UNITY_HOME/.env" ]]; then
    cp "$UNITY_HOME/.env.example" "$UNITY_HOME/.env"
    # Expand ${HOME} for workspace path
    sed -i.bak "s|\${HOME}|$HOME|g" "$UNITY_HOME/.env" 2>/dev/null || \
      sed -i '' "s|\${HOME}|$HOME|g" "$UNITY_HOME/.env"
    rm -f "$UNITY_HOME/.env.bak"
  fi
  for file in comms_twilio.env comms_sa.json; do
    if [[ -e "$UNITY_HOME/$file" && ! -f "$UNITY_HOME/$file" ]]; then
      log_err "Refusing secret path collision: $UNITY_HOME/$file"
      return 1
    fi
    touch "$UNITY_HOME/$file"
  done
  chmod 0600 \
    "$UNITY_HOME/.env" \
    "$UNITY_HOME/comms_twilio.env" \
    "$UNITY_HOME/comms_sa.json"
  log_ok "Compose bundle installed to $UNITY_HOME"
}

upsert_env_value() {
  local env_file="$1"
  local key="$2"
  local val="$3"
  local temporary="${env_file}.$$"
  local replaced=false line
  : >"$temporary"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$key="* ]]; then
      if [[ "$replaced" == "false" ]]; then
        printf '%s=%s\n' "$key" "$val" >>"$temporary"
        replaced=true
      fi
    else
      printf '%s\n' "$line" >>"$temporary"
    fi
  done <"$env_file"
  if [[ "$replaced" == "false" ]]; then
    printf '%s=%s\n' "$key" "$val" >>"$temporary"
  fi
  chmod 0600 "$temporary"
  mv "$temporary" "$env_file"
}

normalize_env_file() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0
  local temporary="${env_file}.$$"
  awk '
    function is_secret(key) {
      return key == "POSTGRES_PASSWORD" ||
        key == "ORCHESTRA_ADMIN_KEY" ||
        key == "NEXTAUTH_SECRET" ||
        key == "JWT_SECRET"
    }
    /^[[:space:]]*#/ || index($0, "=") == 0 { print; next }
    {
      key = $0
      sub(/=.*/, "", key)
      value = substr($0, index($0, "=") + 1)
      if (is_secret(key) && value == "") next
      if (seen[key]++) next
      print
    }
  ' "$env_file" >"$temporary"
  chmod 0600 "$temporary"
  mv "$temporary" "$env_file"
}

generate_secrets() {
  local env_file="$UNITY_HOME/.env"
  normalize_env_file "$env_file"
  if ! grep -qE '^ORCHESTRA_ADMIN_KEY=.+$' "$env_file" 2>/dev/null; then
    upsert_env_value "$env_file" "ORCHESTRA_ADMIN_KEY" \
      "$(openssl rand -base64 32 | tr -d '/+=' | head -c 43)"
  fi
  if ! grep -qE '^NEXTAUTH_SECRET=.+$' "$env_file" 2>/dev/null; then
    upsert_env_value "$env_file" "NEXTAUTH_SECRET" "$(openssl rand -base64 32)"
  fi
  if ! grep -qE '^JWT_SECRET=.+$' "$env_file" 2>/dev/null; then
    upsert_env_value "$env_file" "JWT_SECRET" "$(openssl rand -base64 32)"
  fi
  if ! grep -qE '^POSTGRES_PASSWORD=.+$' "$env_file" 2>/dev/null; then
    upsert_env_value "$env_file" "POSTGRES_PASSWORD" "$(openssl rand -hex 16)"
  fi
  if ! grep -qE '^INTEGRATION_CONFIRMATION_SECRET=.+$' "$env_file" 2>/dev/null; then
    upsert_env_value "$env_file" "INTEGRATION_CONFIRMATION_SECRET" "$(openssl rand -hex 32)"
  fi
}

run_byok_wizard() {
  local wizard="${SCRIPT_DIR:+$SCRIPT_DIR/prompt_byok_keys.sh}"
  if [[ -z "$wizard" || ! -f "$wizard" ]]; then
    local tmp_wizard="$UNITY_HOME/.prompt_byok_keys.sh"
    fetch_remote "selfhost/prompt_byok_keys.sh" "$tmp_wizard"
    wizard="$tmp_wizard"
  fi
  if [[ ! -f "$wizard" ]]; then
    log_warn "BYOK wizard not found — add API keys to $UNITY_HOME/.env manually"
    return 0
  fi
  log_info "BYOK wizard (keys written to $UNITY_HOME/.env)..."
  UNITY_ENV_FILE="$UNITY_HOME/.env" \
    UNITY_COMPOSE_INSTALL=1 \
    NON_INTERACTIVE="$NON_INTERACTIVE" \
    bash "$wizard"
}

create_compose_cli() {
  local compose_cli="${SCRIPT_DIR:+$SCRIPT_DIR/compose-cli.sh}"
  if [[ -z "$compose_cli" || ! -f "$compose_cli" ]]; then
    compose_cli="$UNITY_HOME/compose-cli.sh"
    fetch_remote "selfhost/compose-cli.sh" "$compose_cli"
  fi
  INSTALLED_COMPOSE_CLI="$compose_cli"
  [[ "$CREATE_CLI" == "true" ]] || return 0
  mkdir -p "$CLI_DIR"
  local shim="$CLI_DIR/unify"
  cat > "$shim" <<EOF
#!/usr/bin/env bash
set -e
UNITY_HOME="${UNITY_HOME}"
export UNITY_HOME
COMPOSE_CLI="${compose_cli}"

if [[ -f "\$UNITY_HOME/docker-compose.yml" ]]; then
  cmd="\${1:-up}"
  case "\$cmd" in
    stack)
      shift || true
      sub="\${1:-up}"
      shift || true
      exec bash "\$COMPOSE_CLI" "\$sub" "\$@" ;;
    ""|up)             shift || true; exec bash "\$COMPOSE_CLI" up "\$@" ;;
    down|stop)         shift || true; exec bash "\$COMPOSE_CLI" down "\$@" ;;
    restart)           shift || true; exec bash "\$COMPOSE_CLI" restart "\$@" ;;
    doctor)            shift || true; exec bash "\$COMPOSE_CLI" doctor "\$@" ;;
    status|ps)         shift || true; exec bash "\$COMPOSE_CLI" status "\$@" ;;
    logs)              shift || true; exec bash "\$COMPOSE_CLI" logs "\$@" ;;
    pull)              shift || true; exec bash "\$COMPOSE_CLI" pull "\$@" ;;
    integrations-sync) shift || true; exec bash "\$COMPOSE_CLI" integrations-sync "\$@" ;;
    builtins-sync) shift || true; exec bash "\$COMPOSE_CLI" builtins-sync "\$@" ;;
    setup)
      echo "Compose install is already configured at \$UNITY_HOME" >&2
      echo "Edit \$UNITY_HOME/.env for keys, then run: unify restart" >&2
      exit 0 ;;
    help|-h|--help)
      cat <<'USAGE'
unify — Docker Compose self-host control

  unify                    Start the stack (alias: unify up, unify stack up)
  unify down               Stop the Console UI; runtime keeps running
  unify down --full        Stop every service
  unify restart            Recreate containers after editing ~/.unity/.env
  unify status             Show container status
  unify logs [service...]  Follow logs (optionally for specific services)
  unify pull               Pull the latest images
  unify doctor             Check Docker, keys, and service health
  unify integrations-sync  Sync the Composio app catalog (needs COMPOSIO_API_KEY)
  unify builtins-sync      Retry the Builtins catalogue seed (background)

Edit keys in ~/.unity/.env, then run: unify restart
USAGE
      exit 0 ;;
    *)
      echo "unify: unknown command '\$cmd'" >&2
      echo "Run 'unify help' to see available commands." >&2
      exit 1 ;;
  esac
fi

echo "Compose stack not found. Re-run the installer." >&2
exit 1
EOF
  chmod +x "$shim"
  log_ok "Installed unify CLI at $shim"
}

# Compose gives the caller's shell environment precedence over --env-file
# values during ${VAR} interpolation, so stray exports (direnv, dotfiles, CI)
# would silently override the stack's secrets. Run compose under a minimal
# environment so $UNITY_HOME/.env is the single source of truth.
compose_cmd() {
  local profile_args=()
  local comms_enabled calls_enabled
  comms_enabled="$(grep -E '^SELF_HOST_INTERNAL_COMMS_ENABLED=' "$UNITY_HOME/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr '[:upper:]' '[:lower:]')"
  calls_enabled="$(grep -E '^SELF_HOST_INTERNAL_CALLS_ENABLED=' "$UNITY_HOME/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr '[:upper:]' '[:lower:]')"
  case "$comms_enabled:$calls_enabled" in
    1:*|true:*|yes:*|on:*|*:1|*:true|*:yes|*:on)
      profile_args+=(--profile internal-comms)
      ;;
  esac
  case "$calls_enabled" in
    1|true|yes|on) profile_args+=(--profile internal-calls) ;;
  esac
  env -i \
    PATH="$PATH" \
    HOME="$HOME" \
    TERM="${TERM:-}" \
    ${DOCKER_HOST:+DOCKER_HOST="$DOCKER_HOST"} \
    ${DOCKER_CONFIG:+DOCKER_CONFIG="$DOCKER_CONFIG"} \
    ${DOCKER_CONTEXT:+DOCKER_CONTEXT="$DOCKER_CONTEXT"} \
    ${DOCKER_CERT_PATH:+DOCKER_CERT_PATH="$DOCKER_CERT_PATH"} \
    ${DOCKER_TLS_VERIFY:+DOCKER_TLS_VERIFY="$DOCKER_TLS_VERIFY"} \
    docker compose -f "$UNITY_HOME/docker-compose.yml" --env-file "$UNITY_HOME/.env" \
      "${profile_args[@]}" "$@"
}

verify_orchestra_seed() {
  local seed_status=""
  log_info "Waiting for orchestra billing seed..."
  local i
  for i in $(seq 1 45); do
    seed_status="$(compose_cmd ps -a --format '{{.Service}}\t{{.State}}\t{{.ExitCode}}' 2>/dev/null \
      | awk '$1=="orchestra-seed"{print $2"\t"$3; exit}')"
    if [[ "$seed_status" == "exited	0" ]]; then
      log_ok "Orchestra billing seed completed"
      return 0
    fi
    if [[ "$seed_status" == exited* ]] && [[ "$seed_status" != "exited	0" ]]; then
      log_err "orchestra-seed failed — billing tables were not seeded"
      log_info "Retry: docker compose -f $UNITY_HOME/docker-compose.yml --env-file $UNITY_HOME/.env run --rm orchestra-seed"
      exit 1
    fi
    sleep 2
  done
  log_warn "orchestra-seed status unclear — run: unify stack doctor"
}

start_composio_catalog_sync() {
  if ! grep -qE '^COMPOSIO_API_KEY=.+$' "$UNITY_HOME/.env" 2>/dev/null; then
    return 0
  fi
  log_info "Composio integration catalogue sync runs via unity-builtins-seed (watch: unify stack logs unity-builtins-seed)"
}

pull_and_start() {
  log_info "Pulling images (first run may take several minutes)..."
  compose_cmd pull
  log_info "Starting stack..."
  if ! UNITY_HOME="$UNITY_HOME" bash "$INSTALLED_COMPOSE_CLI" up; then
    log_err "Stack startup failed and was rolled back"
    return 1
  fi
  verify_orchestra_seed
  start_composio_catalog_sync
  log_ok "Stack started"
}

open_browser() {
  local url
  url="$(grep -E '^NEXTAUTH_URL=' "$UNITY_HOME/.env" 2>/dev/null | cut -d= -f2- || echo 'http://127.0.0.1:3000')"
  log_info "Open $url to register and chat with your Coordinator"
  case "$(uname -s)" in
    Darwin) open "$url" 2>/dev/null || true ;;
    Linux) xdg-open "$url" 2>/dev/null || true ;;
  esac
}

main() {
  print_banner
  require_docker
  install_compose_bundle
  generate_secrets
  run_byok_wizard
  create_compose_cli
  if [[ "${UNITY_COMPOSE_SKIP_START:-0}" == "1" ]]; then
    log_ok "Skipping image pull/start (UNITY_COMPOSE_SKIP_START=1)"
  else
    pull_and_start
    open_browser
  fi
  echo ""
  log_ok "Installation complete"
  echo ""
  echo "  Daily driver:  unify / unify stack up"
  echo "  UI off:        unify stack down"
  echo "  Stop all:      unify stack down --full"
  echo "  Edit keys:     \$UNITY_HOME/.env  then  unify restart"
  if grep -qE '^COMPOSIO_API_KEY=.+$' "$UNITY_HOME/.env" 2>/dev/null; then
    echo "  Integrations:  catalog sync runs in background (~30 min); unify stack doctor"
  fi
  if [[ "$(uname -s)" == "Darwin" ]]; then
    echo ""
    echo "  macOS — let Twin control THIS Mac (not only the Docker desktop):"
    echo "    1. Install Unify Desktop Assistant (.pkg):"
    echo "       https://github.com/unifyai/unify-desktop-assistant/releases"
    echo "    2. Menu bar app → Settings → paste your API key (from Console → Connect your desktop)"
    echo "    3. Approve Screen Sharing when prompted; wait for green status"
    echo "    4. Console → Connect your desktop → link your Mac → unify restart"
    echo "    Full guide: \$UNITY_HOME/README.md"
  fi
  echo ""
}

main "$@"
