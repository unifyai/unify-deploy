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
  local stamp unity_home python_bin
  stamp="$(date +%Y%m%d-%H%M%S)"
  unity_home="${FRESH_INSTALL_ROOT}/unity-compose-smoke-${stamp}"
  python_bin="${PYTHON_BIN:-$DEPLOY_REPO/.venv/bin/python}"
  [[ -x "$python_bin" ]] || python_bin="$(command -v python3)"
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

  local required_file
  for required_file in \
    Caddyfile \
    call-controller-entrypoint.sh \
    call-proxy.Caddyfile \
    cm-entrypoint.sh \
    comms-bridge-entrypoint.sh \
    comms_ingress_bridge.py \
    coordinator.env \
    docker-compose.yml \
    gateway-entrypoint.sh \
    load-comms-secrets.sh \
    provision_call_sip.py \
    sync_comms_webhooks.py; do
    if [[ ! -f "$unity_home/$required_file" ]]; then
      fail "Compose bundle missing $required_file"
      return 1
    fi
  done
  ok "communications bundle installed"

  local mode
  if mode="$(stat -f '%Lp' "$unity_home" 2>/dev/null)"; then
    :
  else
    mode="$(stat -c '%a' "$unity_home")"
  fi
  [[ "$mode" == "700" ]] || {
    fail "UNITY_HOME permissions are $mode, expected 700"
    return 1
  }
  for required_file in .env comms_sa.json comms_twilio.env; do
    if mode="$(stat -f '%Lp' "$unity_home/$required_file" 2>/dev/null)"; then
      :
    else
      mode="$(stat -c '%a' "$unity_home/$required_file")"
    fi
    [[ "$mode" == "600" ]] || {
      fail "$required_file permissions are $mode, expected 600"
      return 1
    }
  done
  for required_file in cm-entrypoint.sh comms-bridge-entrypoint.sh call-controller-entrypoint.sh; do
    if mode="$(stat -f '%Lp' "$unity_home/$required_file" 2>/dev/null)"; then
      :
    else
      mode="$(stat -c '%a' "$unity_home/$required_file")"
    fi
    [[ "$mode" == "755" ]] || {
      fail "$required_file permissions are $mode, expected 755"
      return 1
    }
  done
  for required_file in comms_ingress_bridge.py provision_call_sip.py sync_comms_webhooks.py; do
    if mode="$(stat -f '%Lp' "$unity_home/$required_file" 2>/dev/null)"; then
      :
    else
      mode="$(stat -c '%a' "$unity_home/$required_file")"
    fi
    [[ "$mode" == "644" ]] || {
      fail "$required_file permissions are $mode, expected 644"
      return 1
    }
  done
  ok "local state and secret permissions hardened"

  log "Validating compose file..."
  local compose=(
    docker compose
    -f "$unity_home/docker-compose.yml"
    --env-file "$unity_home/.env"
  )
  if ! "${compose[@]}" config >/dev/null; then
    fail "docker compose config failed"
    return 1
  fi
  ok "compose config valid"

  local default_services
  default_services="$("${compose[@]}" config --services)"
  if grep -Eq '^(comms-bridge|call-controller|call-proxy|call-tunnel)$' <<<"$default_services"; then
    fail "Optional communications services rendered by default"
    return 1
  fi
  ok "optional communications profiles disabled by default"

  "$python_bin" - "$unity_home/.env" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
values = {
    "SELF_HOST_INTERNAL_COMMS_ENABLED": "true",
    "SELF_HOST_INTERNAL_CALLS_ENABLED": "true",
    "LIVEKIT_URL": "wss://smoke.invalid",
    "LIVEKIT_API_KEY": "smoke-livekit-key",
    "LIVEKIT_API_SECRET": "smoke-livekit-secret",
    "LIVEKIT_SIP_URI": "smoke.sip.invalid",
}
lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
result = []
for line in lines:
    key = line.partition("=")[0]
    if key in values:
        result.append(f"{key}={values[key]}")
        seen.add(key)
    else:
        result.append(line)
for key, value in values.items():
    if key not in seen:
        result.append(f"{key}={value}")
path.write_text("\n".join(result) + "\n", encoding="utf-8")
PY
  cat >"$unity_home/comms_twilio.env" <<'EOF'
TWILIO_ACCOUNT_SID=ACsmoke
TWILIO_AUTH_TOKEN=smoke-main-token
TWILIO_WA_ACCOUNT_SID=ACwasmoke
TWILIO_WA_AUTH_TOKEN=smoke-wa-token
EOF
  chmod 0600 "$unity_home/.env" "$unity_home/comms_twilio.env"

  local profile_services
  profile_services="$(
    "${compose[@]}" \
      --profile internal-comms \
      --profile internal-calls \
      config --services
  )"
  for required_file in comms-bridge call-controller call-proxy call-tunnel; do
    if ! grep -qx "$required_file" <<<"$profile_services"; then
      fail "Enabled communications profile missing $required_file"
      return 1
    fi
  done
  ok "communications and calls profiles render"

  "${compose[@]}" \
    --profile internal-comms \
    --profile internal-calls \
    config --format json \
    | "$python_bin" -c '
import json
import sys

config = json.load(sys.stdin)
services = config["services"]
expected_identity = "local-twin@unify.ai"
for service in ("orchestra", "gateway", "unity-cm", "comms-bridge"):
    env = services[service]["environment"]
    if expected_identity not in env.values():
        raise SystemExit(f"{service} missing Coordinator email identity")

for service, secret in (
    ("gateway", "comms_gmail"),
    ("gateway", "comms_twilio"),
    ("unity-cm", "comms_twilio"),
    ("comms-bridge", "comms_gmail"),
    ("comms-bridge", "comms_twilio"),
):
    mounted = {entry["source"]: entry for entry in services[service]["secrets"]}
    if secret not in mounted:
        raise SystemExit(f"{service} missing {secret} secret")

gateway_volumes = services["gateway"]["volumes"]
if not any(
    volume.get("source") == "runtime" and volume.get("target") == "/runtime"
    for volume in gateway_volumes
):
    raise SystemExit("gateway cannot read the runtime tunnel URL")

for service in ("console", "desktop-proxy", "unity-cm"):
    for binding in services[service].get("ports", []):
        if binding.get("host_ip") != "127.0.0.1":
            raise SystemExit(f"{service} has non-loopback published port")
'
  ok "identity, secret mounts, and loopback bindings render correctly"

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
