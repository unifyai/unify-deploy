#!/usr/bin/env bash
# =============================================================================
# fresh_install_smoke.sh — Exercise the stranger install path in an isolated env
# =============================================================================
#
# Simulates a fresh install without touching ~/.unity or ~/Unify dev trees.
#
# Usage:
#   ./scripts/fresh_install_smoke.sh                 # local isolated dir
#   ./scripts/fresh_install_smoke.sh --docker        # Ubuntu container (cleanest)
#   ./scripts/fresh_install_smoke.sh --compose       # docker compose bundle + config
#   ./scripts/fresh_install_smoke.sh --source-install  # legacy source clone path
#   ./scripts/fresh_install_smoke.sh --branch staging
#
# Environment:
#   FRESH_INSTALL_ROOT   Parent dir for test installs (default: /tmp)
#   FRESH_INSTALL_BRANCH Git branch to install (default: staging)
#   ENSURE_PREREQS_AUTO_INSTALL=0  Disable auto-install during smoke test
#
set -euo pipefail

# This script lives in unity-deploy/selfhost/. DEPLOY_REPO is the unity-deploy
# checkout; UNITY_DEV_REPO is the sibling unify checkout under UNIFY_STACK_ROOT.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=default_repo_paths.sh
source "$SCRIPT_DIR/default_repo_paths.sh"
DEPLOY_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIFY_STACK_ROOT="${UNIFY_STACK_ROOT:-$(cd "$DEPLOY_REPO/.." && pwd)}"
UNITY_DEV_REPO="${UNITY_REPO_PATH:-$(default_unity_repo_path "$UNIFY_STACK_ROOT")}"
export UNITY_REPO_PATH="${UNITY_REPO_PATH:-$UNITY_DEV_REPO}"
INSTALL_SCRIPT="${INSTALL_SCRIPT:-$UNITY_DEV_REPO/scripts/install.sh}"

FRESH_INSTALL_ROOT="${FRESH_INSTALL_ROOT:-/tmp}"
FRESH_INSTALL_BRANCH="${FRESH_INSTALL_BRANCH:-staging}"
USE_DOCKER=false
CODE_ONLY=false
COMPOSE_ONLY=false
SOURCE_INSTALL=true
KEEP=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --docker) USE_DOCKER=true; shift ;;
    --code-only) CODE_ONLY=true; shift ;;
    --compose) COMPOSE_ONLY=true; shift ;;
    --source-install) SOURCE_INSTALL=true; shift ;;
    --branch) FRESH_INSTALL_BRANCH="$2"; shift 2 ;;
    --keep) KEEP=true; shift ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

log() { printf '→ %s\n' "$1"; }
ok() { printf '✓ %s\n' "$1"; }
fail() { printf '✗ %s\n' "$1" >&2; }

run_compose_smoke() {
  local stamp unity_home
  stamp="$(date +%Y%m%d-%H%M%S)"
  unity_home="${FRESH_INSTALL_ROOT}/unity-compose-smoke-${stamp}"
  mkdir -p "$unity_home"

  log "Compose install smoke"
  log "  UNITY_HOME=$unity_home"

  export NON_INTERACTIVE=true
  export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-smoke-test-placeholder}"

  if ! UNITY_COMPOSE_SKIP_START=1 \
    INSTALL_SELFHOST_SRC="$DEPLOY_REPO/deploy/selfhost" \
    UNITY_HOME="$unity_home" \
    bash "$SCRIPT_DIR/install-compose.sh" --dir "$unity_home" --no-cli --non-interactive; then
    fail "install-compose.sh failed"
    [[ "$KEEP" == "true" ]] || rm -rf "$unity_home"
    return 1
  fi

  log "Validating compose file..."
  if ! docker compose -f "$unity_home/docker-compose.yml" --env-file "$unity_home/.env" config >/dev/null; then
    fail "docker compose config failed"
    return 1
  fi
  ok "compose config valid"

  [[ "$KEEP" == "true" ]] && log "Keeping install at $unity_home (--keep)" || rm -rf "$unity_home"
  ok "Compose smoke passed"
}

run_local_smoke() {
  local stamp unity_home
  stamp="$(date +%Y%m%d-%H%M%S)"
  unity_home="${FRESH_INSTALL_ROOT}/unity-fresh-smoke-${stamp}"
  mkdir -p "$unity_home"

  log "Fresh install smoke (local)"
  log "  UNITY_HOME=$unity_home"
  log "  branch=$FRESH_INSTALL_BRANCH"
  log "  install script=$INSTALL_SCRIPT"

  local install_args=(--dir "$unity_home" --branch "$FRESH_INSTALL_BRANCH" --no-cli --skip-setup)
  if [[ "$SOURCE_INSTALL" == "true" ]]; then
    install_args+=(--source-install)
  fi

  export NON_INTERACTIVE=true
  export ORCHESTRA_PREFIX="unity-smoke-${stamp: -8}"
  export ORCHESTRA_DB_PORT=$((56000 + RANDOM % 500))
  export ORCHESTRA_PORT=$((8200 + RANDOM % 200))

  if ! bash "$INSTALL_SCRIPT" "${install_args[@]}"; then
    fail "install.sh failed"
    [[ "$KEEP" == "true" ]] || rm -rf "$unity_home"
    return 1
  fi

  log "Overlaying local unity/scripts for pre-push validation..."
  rsync -a "$UNITY_DEV_REPO/scripts/" "$unity_home/unity/scripts/"
  if [[ -f "$HOME/Unify/orchestra/scripts/local.sh" ]]; then
    log "Overlaying local orchestra/scripts/local.sh for pre-push validation..."
    rsync -a "$HOME/Unify/orchestra/scripts/local.sh" "$unity_home/orchestra/scripts/local.sh" 2>/dev/null || true
  fi

  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export UNITY_HOME="$unity_home"

  if [[ "$CODE_ONLY" == "true" ]]; then
    log "Running unity stack doctor (expected gaps before setup)..."
    if bash "$unity_home/unity/scripts/stack.sh" doctor; then
      ok "stack doctor passed"
    else
      log "Doctor reported expected gaps for --code-only (orchestra/.env.local/LLM come from unity setup)"
    fi
    if [[ "$KEEP" == "true" ]]; then
      log "Keeping install at $unity_home (--keep)"
    else
      rm -rf "$unity_home"
    fi
    ok "code-only smoke passed (clone + infra prereqs)"
    return 0
  fi

  log "Running unity setup (orchestra + console env + npm)..."
  log "  isolated orchestra: prefix=$ORCHESTRA_PREFIX port=$ORCHESTRA_PORT db=$ORCHESTRA_DB_PORT"
  if [[ -z "${OPENAI_API_KEY:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
    local dev_env="$HOME/Unify/unity/.env"
    if [[ -f "$dev_env" ]]; then
      OPENAI_API_KEY="$(grep -E '^OPENAI_API_KEY=' "$dev_env" | head -1 | cut -d= -f2- || true)"
      ANTHROPIC_API_KEY="$(grep -E '^ANTHROPIC_API_KEY=' "$dev_env" | head -1 | cut -d= -f2- || true)"
      export OPENAI_API_KEY ANTHROPIC_API_KEY
    fi
  fi
  if [[ -z "${OPENAI_API_KEY:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
    export OPENAI_API_KEY="sk-smoke-test-placeholder" # pragma: allowlist secret
    log "No LLM key in env — using placeholder for smoke test (chat will not work until a real key is set)"
  fi
    if ! UNITY_HOME="$unity_home" UNITY_BRANCH="$FRESH_INSTALL_BRANCH" \
      ORCHESTRA_PREFIX="$ORCHESTRA_PREFIX" \
      ORCHESTRA_DB_PORT="$ORCHESTRA_DB_PORT" \
      ORCHESTRA_PORT="$ORCHESTRA_PORT" \
      PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" \
      bash "$unity_home/unity/scripts/setup.sh"; then
    fail "unity setup failed"
    [[ "$KEEP" == "true" ]] || rm -rf "$unity_home"
    return 1
  fi
  ok "unity setup completed"

  log "Running unity stack doctor..."
  if ! bash "$unity_home/unity/scripts/stack.sh" doctor; then
    fail "stack doctor failed after setup"
    [[ "$KEEP" == "true" ]] || rm -rf "$unity_home"
    return 1
  fi
  ok "stack doctor passed"

  if [[ "$KEEP" == "true" ]]; then
    log "Keeping install at $unity_home (--keep)"
  else
    log "Cleaning up $unity_home"
    rm -rf "$unity_home"
  fi
  ok "Fresh install smoke passed"
}

run_docker_smoke() {
  if ! command -v docker >/dev/null 2>&1; then
    fail "docker not available — run without --docker or install Docker Desktop"
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    fail "Docker daemon is not running"
    return 1
  fi

  local stamp unity_home_in_container
  stamp="$(date +%Y%m%d-%H%M%S)"
  unity_home_in_container="/tmp/unity-fresh-smoke-${stamp}"

  log "Fresh install smoke (Docker Ubuntu)"
  log "  container UNITY_HOME=$unity_home_in_container"
  log "  branch=$FRESH_INSTALL_BRANCH"

  local skip_setup_flag=""
  [[ "$CODE_ONLY" == "true" ]] && skip_setup_flag="--skip-setup"

  # Mount local install.sh so we test script changes before push.
  docker run --rm \
    -e DEBIAN_FRONTEND=noninteractive \
    -e ENSURE_PREREQS_AUTO_INSTALL="${ENSURE_PREREQS_AUTO_INSTALL:-1}" \
    -v "$INSTALL_SCRIPT:/install.sh:ro" \
    ubuntu:24.04 \
    bash -lc "
      set -euo pipefail
      apt-get update -qq
      apt-get install -y -qq curl git ca-certificates sudo build-essential python3 python3-dev portaudio19-dev pkg-config docker.io >/dev/null
      service docker start >/dev/null 2>&1 || true
      sleep 2
      curl -fsSL https://astral.sh/uv/install.sh | sh >/dev/null
      export PATH=\"\$HOME/.local/bin:\$PATH\"
      bash /install.sh --dir '$unity_home_in_container' --branch '$FRESH_INSTALL_BRANCH' $skip_setup_flag
      export UNITY_HOME='$unity_home_in_container'
      bash \"\$UNITY_HOME/unity/scripts/stack.sh\" doctor
      echo '✓ Docker fresh install smoke passed'
    "
}

main() {
  if [[ "$COMPOSE_ONLY" == "true" ]]; then
    run_compose_smoke
  elif [[ "$USE_DOCKER" == "true" ]]; then
    run_docker_smoke
  else
    run_local_smoke
  fi
}

main "$@"
