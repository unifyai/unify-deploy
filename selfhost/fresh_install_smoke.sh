#!/usr/bin/env bash
# =============================================================================
# fresh_install_smoke.sh — Exercise the stranger install path in an isolated env
# =============================================================================
#
# Simulates a fresh install without touching ~/.droid or ~/Unify dev trees.
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
# checkout; DROID_DEV_REPO is the sibling droid checkout under UNIFY_STACK_ROOT.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIFY_STACK_ROOT="${UNIFY_STACK_ROOT:-$(cd "$DEPLOY_REPO/.." && pwd)}"
DROID_DEV_REPO="${DROID_REPO_PATH:-$UNIFY_STACK_ROOT/droid}"
INSTALL_SCRIPT="${INSTALL_SCRIPT:-$DROID_DEV_REPO/scripts/install.sh}"

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
  local stamp droid_home
  stamp="$(date +%Y%m%d-%H%M%S)"
  droid_home="${FRESH_INSTALL_ROOT}/droid-compose-smoke-${stamp}"
  mkdir -p "$droid_home"

  log "Compose install smoke"
  log "  DROID_HOME=$droid_home"

  export NON_INTERACTIVE=true
  export OPENAI_API_KEY="${OPENAI_API_KEY:-sk-smoke-test-placeholder}"

  if ! DROID_COMPOSE_SKIP_START=1 \
    INSTALL_SELFHOST_SRC="$DEPLOY_REPO/deploy/selfhost" \
    DROID_HOME="$droid_home" \
    bash "$SCRIPT_DIR/install-compose.sh" --dir "$droid_home" --no-cli --non-interactive; then
    fail "install-compose.sh failed"
    [[ "$KEEP" == "true" ]] || rm -rf "$droid_home"
    return 1
  fi

  log "Validating compose file..."
  if ! docker compose -f "$droid_home/docker-compose.yml" --env-file "$droid_home/.env" config >/dev/null; then
    fail "docker compose config failed"
    return 1
  fi
  ok "compose config valid"

  [[ "$KEEP" == "true" ]] && log "Keeping install at $droid_home (--keep)" || rm -rf "$droid_home"
  ok "Compose smoke passed"
}

run_local_smoke() {
  local stamp droid_home
  stamp="$(date +%Y%m%d-%H%M%S)"
  droid_home="${FRESH_INSTALL_ROOT}/droid-fresh-smoke-${stamp}"
  mkdir -p "$droid_home"

  log "Fresh install smoke (local)"
  log "  DROID_HOME=$droid_home"
  log "  branch=$FRESH_INSTALL_BRANCH"
  log "  install script=$INSTALL_SCRIPT"

  local install_args=(--dir "$droid_home" --branch "$FRESH_INSTALL_BRANCH" --no-cli --skip-setup)
  if [[ "$SOURCE_INSTALL" == "true" ]]; then
    install_args+=(--source-install)
  fi

  export NON_INTERACTIVE=true
  export ORCHESTRA_PREFIX="droid-smoke-${stamp: -8}"
  export ORCHESTRA_DB_PORT=$((56000 + RANDOM % 500))
  export ORCHESTRA_PORT=$((8200 + RANDOM % 200))

  if ! bash "$INSTALL_SCRIPT" "${install_args[@]}"; then
    fail "install.sh failed"
    [[ "$KEEP" == "true" ]] || rm -rf "$droid_home"
    return 1
  fi

  log "Overlaying local droid/scripts for pre-push validation..."
  rsync -a "$DROID_DEV_REPO/scripts/" "$droid_home/droid/scripts/"
  if [[ -f "$HOME/Unify/orchestra/scripts/local.sh" ]]; then
    log "Overlaying local orchestra/scripts/local.sh for pre-push validation..."
    rsync -a "$HOME/Unify/orchestra/scripts/local.sh" "$droid_home/orchestra/scripts/local.sh" 2>/dev/null || true
  fi

  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  export DROID_HOME="$droid_home"

  if [[ "$CODE_ONLY" == "true" ]]; then
    log "Running droid stack doctor (expected gaps before setup)..."
    if bash "$droid_home/droid/scripts/stack.sh" doctor; then
      ok "stack doctor passed"
    else
      log "Doctor reported expected gaps for --code-only (orchestra/.env.local/LLM come from droid setup)"
    fi
    if [[ "$KEEP" == "true" ]]; then
      log "Keeping install at $droid_home (--keep)"
    else
      rm -rf "$droid_home"
    fi
    ok "code-only smoke passed (clone + infra prereqs)"
    return 0
  fi

  log "Running droid setup (orchestra + console env + npm)..."
  log "  isolated orchestra: prefix=$ORCHESTRA_PREFIX port=$ORCHESTRA_PORT db=$ORCHESTRA_DB_PORT"
  if [[ -z "${OPENAI_API_KEY:-}" && -z "${ANTHROPIC_API_KEY:-}" ]]; then
    local dev_env="$HOME/Unify/droid/.env"
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
    if ! DROID_HOME="$droid_home" DROID_BRANCH="$FRESH_INSTALL_BRANCH" \
      ORCHESTRA_PREFIX="$ORCHESTRA_PREFIX" \
      ORCHESTRA_DB_PORT="$ORCHESTRA_DB_PORT" \
      ORCHESTRA_PORT="$ORCHESTRA_PORT" \
      PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" \
      bash "$droid_home/droid/scripts/setup.sh"; then
    fail "droid setup failed"
    [[ "$KEEP" == "true" ]] || rm -rf "$droid_home"
    return 1
  fi
  ok "droid setup completed"

  log "Running droid stack doctor..."
  if ! bash "$droid_home/droid/scripts/stack.sh" doctor; then
    fail "stack doctor failed after setup"
    [[ "$KEEP" == "true" ]] || rm -rf "$droid_home"
    return 1
  fi
  ok "stack doctor passed"

  if [[ "$KEEP" == "true" ]]; then
    log "Keeping install at $droid_home (--keep)"
  else
    log "Cleaning up $droid_home"
    rm -rf "$droid_home"
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

  local stamp droid_home_in_container
  stamp="$(date +%Y%m%d-%H%M%S)"
  droid_home_in_container="/tmp/droid-fresh-smoke-${stamp}"

  log "Fresh install smoke (Docker Ubuntu)"
  log "  container DROID_HOME=$droid_home_in_container"
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
      bash /install.sh --dir '$droid_home_in_container' --branch '$FRESH_INSTALL_BRANCH' $skip_setup_flag
      export DROID_HOME='$droid_home_in_container'
      bash \"\$DROID_HOME/droid/scripts/stack.sh\" doctor
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
