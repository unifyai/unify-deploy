#!/usr/bin/env bash
# =============================================================================
# stack.sh — Self-host stack
# =============================================================================
#
# Brings up Orchestra, droid.gateway, Pub/Sub emulator, Console, and
# the Droid CM for the signed-in user's Coordinator when credentials exist.
#
# Usage:
#   ./scripts/stack.sh up           Start full stack (+ Coordinator if registered)
#   ./scripts/stack.sh down [--full]    Stop stack (--full stops background runtime too)
#   ./scripts/stack.sh status       Show service status
#   ./scripts/stack.sh logs [svc]   Follow service logs (console|orchestra|pubsub)
#   ./scripts/stack.sh doctor       Check prerequisites
#   ./scripts/stack.sh smoke        Verify the running local product
#   ./scripts/stack.sh repair-console  Restart Console with preserved stack env
#   ./scripts/stack.sh dev-env      Print non-secret Console env expected by stack
#
# Environment:
#   UNIFY_STACK_ROOT          Parent dir with orchestra/console/droid siblings
#   OPENAI_API_KEY / ANTHROPIC_API_KEY  Required for Coordinator chat
#   DEEPGRAM_API_KEY / CARTESIA_API_KEY Required for browser calls (prompted by droid setup)
#
set -euo pipefail

# This script lives in droid-deploy/selfhost/. The self-host stack orchestrates
# the sibling droid, console, and orchestra checkouts located under
# UNIFY_STACK_ROOT (defaults to the parent of droid-deploy).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEPLOY_REPO_PATH="$(cd "$SCRIPT_DIR/.." && pwd -P)"
ENSURE_PREREQS_SCRIPT="$SCRIPT_DIR/ensure_prereqs.sh"
SELF_HOST_ENV_SCRIPT="$SCRIPT_DIR/self_host_env.sh"
STACK_STATE_SCRIPT="$SCRIPT_DIR/stack_state.sh"

UNIFY_STACK_ROOT="${UNIFY_STACK_ROOT:-$(cd "$DEPLOY_REPO_PATH/.." && pwd -P)}"
DROID_REPO_PATH="${DROID_REPO_PATH:-$UNIFY_STACK_ROOT/droid}"
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
  local env_file="$DROID_REPO_PATH/.env"
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
  echo "Stranger path: curl install → droid setup → droid → register → chat"
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

  if [[ -f "$DROID_REPO_PATH/.venv/bin/python" ]]; then
    local droid_py="$DROID_REPO_PATH/.venv/bin/python"
    if "$droid_py" -c "import droid.gateway" &>/dev/null; then
      log_success "Droid venv + droid.gateway OK"
    else
      log_error "droid.gateway not importable — run: cd $DROID_REPO_PATH && uv sync"
      ok=false
    fi
  else
    log_warn "Droid .venv missing — run: cd $DROID_REPO_PATH && uv sync"
    ok=false
  fi

  if [[ -f "$CONSOLE_REPO_PATH/.env.local" ]]; then
    log_success "console/.env.local found"
  else
    log_warn "console/.env.local missing — copy from .env.development and set JWT_SECRET"
    ok=false
  fi

  echo ""
  echo "BYOK keys (droid/.env)"
  echo "----------------------"
  echo "  Required: LLM (OpenAI or Anthropic)"
  echo "  Voice:    Deepgram + Cartesia (browser calls; LiveKit auto-configured on stack up)"
  echo "  Optional: Tavily (web search), AntiCaptcha (computer use)"
  echo ""

  if _has_env_key OPENAI_API_KEY || _has_env_key ANTHROPIC_API_KEY; then
    log_success "LLM provider key configured"
  else
    log_error "No LLM API key — run: droid setup (or scripts/prompt_byok_keys.sh)"
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

  if _has_env_key DROID_WEB_TAVILY_API_KEY; then
    log_success "DROID_WEB_TAVILY_API_KEY set (web search)"
  else
    log_info "Web search not configured (optional — Tavily via prompt_byok_keys.sh)"
  fi

  if _has_env_key ANTICAPTCHA_KEY || _has_env_key DROID_ACTOR_ANTICAPTCHA_KEY; then
    log_success "AntiCaptcha key set (computer automation)"
  else
    log_info "AntiCaptcha not configured (optional — computer use / CAPTCHA solving)"
  fi

  echo ""
  echo "Runtime"
  echo "-------"
  log_info "FileManager workspace: ${DROID_LOCAL_ROOT:-$HOME/Droid/Local}"
  log_info "Scheduled tasks: LocalActivationScheduler in Coordinator CM"
  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    self_host_runtime_doctor_line | sed 's/^/  /'
    echo ""
    log_info "Daily driver: droid stack up / droid stack down"
    log_info "Stop everything: droid stack down --full  (or: droid service disable)"
    log_info "Survive reboot without Console: droid setup --boot-runtime"
  else
    log_info "Stack must stay up for scheduled tasks until self-host runtime is wired"
  fi
  log_info "Live Actions stream via EventBus → Pub/Sub actions-sub"

  echo ""
  if [[ "$ok" == "true" ]]; then
    log_success "Doctor passed — run: droid stack up"
    return 0
  fi
  log_error "Doctor found blockers — fix above, then re-run: droid stack doctor"
  return 1
}

cmd_up() {
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

  if [[ -x "$DROID_REPO_PATH/scripts/voice.sh" ]]; then
    log_info "Ensuring local LiveKit + voice BYOK keys..."
    DROID_HOME="${DROID_HOME:-$HOME/.droid}" \
      DROID_REPO="${DROID_REPO:-$DROID_REPO_PATH}" \
      bash "$DROID_REPO_PATH/scripts/voice.sh" setup || log_warn "LiveKit setup failed — meet may not work"
  fi

  if [[ ! -f "$CONSOLE_LOCAL_SCRIPT" ]]; then
    log_error "Missing $CONSOLE_LOCAL_SCRIPT"
    return 1
  fi

  export SELF_HOST=1
  export DEPLOY_REPO_PATH
  export ORCHESTRA_REPO_PATH
  export DROID_REPO_PATH
  export CONSOLE_REPO_PATH
  export ORCHESTRA_DB_PORT="$(default_orchestra_db_port)"
  export DROID_HOME="${DROID_HOME:-$HOME/.droid}"
  export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$DROID_HOME}"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    export_self_host_coordinator_runtime_file
    load_self_host_env_file "$DROID_REPO_PATH/.env"
    if declare -F self_host_enable_runtime &>/dev/null; then
      self_host_enable_runtime
    fi
  fi

  # voice.sh runs a local LiveKit server with dev credentials. droid/.env
  # often also contains cloud LiveKit keys that override the dev pair when
  # sourced, which breaks browser meet token minting in Console.
  export LIVEKIT_URL="ws://localhost:7880"
  export LIVEKIT_API_KEY="devkey"  # pragma: allowlist secret
  export LIVEKIT_API_SECRET="secret"  # pragma: allowlist secret

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
    if declare -F droid_cm_instance_count &>/dev/null; then
      cm_count="$(droid_cm_instance_count)"
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
      log_error "Multiple Coordinator runtimes detected — run: droid stack down --full"
    else
      log_info "Starting Coordinator runtime (saved login)..."
      if ! bash "$CONSOLE_LOCAL_SCRIPT" ensure-coordinator-topics; then
        log_warn "Coordinator Pub/Sub setup failed — sign in at Console to refresh credentials"
      elif declare -F with_droid_runtime_start_lock &>/dev/null; then
        if ! with_droid_runtime_start_lock 30 bash "$CONSOLE_LOCAL_SCRIPT" start-coordinator; then
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
  echo ""
  if [[ -f "$runtime_file" ]]; then
    echo "  Open Console and chat with your Coordinator."
    if declare -F self_host_headless_scheduling_ready &>/dev/null \
      && self_host_headless_scheduling_ready; then
      echo "  stack down stops the UI only — scheduled tasks keep running in the background."
    elif declare -F self_host_service_is_enabled &>/dev/null \
      && self_host_service_is_enabled; then
      echo "  Background runtime is not healthy — stack down stops scheduled tasks."
      echo "  Re-run: droid stack up"
    fi
  else
    echo "  First visit: create an account on /login — Coordinator starts automatically."
  fi
  echo ""
}

cmd_down() {
  local full_stop="false"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --full) full_stop="true"; shift ;;
      -h|--help)
        echo "Usage: droid stack down [--full]"
        echo ""
        echo "  Default: stop Console and stack ingress; keep Coordinator + Orchestra for scheduled tasks."
        echo "  --full:  stop everything, including background runtime."
        echo ""
        echo "  Also: droid service disable  (same as --full for background runtime)"
        return 0
        ;;
      *)
        log_error "Unknown option: $1"
        echo "Run: droid stack down --help"
        return 1
        ;;
    esac
  done

  if [[ ! -f "$CONSOLE_LOCAL_SCRIPT" ]]; then
    log_error "Missing $CONSOLE_LOCAL_SCRIPT"
    return 1
  fi

  export DROID_HOME="${DROID_HOME:-$HOME/.droid}"
  export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$DROID_HOME}"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
  fi

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
  fi
  log_success "Self-host stack stopped"
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
  export DROID_REPO_PATH
  export CONSOLE_REPO_PATH
  export ORCHESTRA_DB_PORT="$(default_orchestra_db_port)"
  export DROID_HOME="${DROID_HOME:-$HOME/.droid}"
  export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$DROID_HOME}"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    export_self_host_coordinator_runtime_file
    load_self_host_env_file "$DROID_REPO_PATH/.env"
  fi

  export LIVEKIT_URL="ws://localhost:7880"
  export LIVEKIT_API_KEY="devkey"  # pragma: allowlist secret
  export LIVEKIT_API_SECRET="secret"  # pragma: allowlist secret

  if ! bash "$CONSOLE_LOCAL_SCRIPT" repair-console --self-host; then
    log_error "Console repair failed"
    return 1
  fi
  if declare -F stack_state_write_source &>/dev/null; then
    stack_state_write_source
  fi
  log_success "Console repaired at http://localhost:${CONSOLE_PORT:-3000}"
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
  export DROID_REPO_PATH
  export CONSOLE_REPO_PATH
  export ORCHESTRA_DB_PORT="${ORCHESTRA_DB_PORT:-55432}"
  export DROID_HOME="${DROID_HOME:-$HOME/.droid}"
  export SELF_HOST_STATE_DIR="${SELF_HOST_STATE_DIR:-$DROID_HOME}"

  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    export_self_host_coordinator_runtime_file
  fi

  local py="$DROID_REPO_PATH/.venv/bin/python"
  if [[ ! -x "$py" ]]; then
    py="python3"
  fi

  CONSOLE_PORT="${CONSOLE_PORT:-3000}" \
    ORCHESTRA_PORT="${ORCHESTRA_PORT:-8000}" \
    DROID_GATEWAY_HOST="${DROID_GATEWAY_HOST:-127.0.0.1}" \
    DROID_GATEWAY_PORT="${DROID_GATEWAY_PORT:-8001}" \
    SELF_HOST_COORDINATOR_RUNTIME_FILE="${SELF_HOST_COORDINATOR_RUNTIME_FILE:-}" \
    "$py" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def log(kind: str, message: str) -> None:
    print(f"[{kind}] {message}")


def request_status(method: str, url: str, *, headers=None, body=None, timeout=15):
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


def check(name: str, method: str, url: str, expected: set[int], *, headers=None, body=None) -> None:
    status, text = request_status(method, url, headers=headers, body=body)
    if status in expected:
        log("OK", f"{name}: HTTP {status}")
        return
    preview = text.replace("\n", " ")[:300]
    failures.append(name)
    log("ERROR", f"{name}: expected {sorted(expected)}, got HTTP {status}. {preview}")


console = f"http://127.0.0.1:{os.environ['CONSOLE_PORT']}"
orchestra = f"http://127.0.0.1:{os.environ['ORCHESTRA_PORT']}"
gateway = f"http://{os.environ['DROID_GATEWAY_HOST']}:{os.environ['DROID_GATEWAY_PORT']}"

check("Console", "GET", console, {200})
check("Orchestra features", "GET", f"{orchestra}/v0/features", {200})
check("Droid gateway", "GET", f"{gateway}/health", {200})
check(
    "Registration path",
    "POST",
    f"{console}/api/auth/email/register",
    {422},
    body={
        "email": "droid-smoke@example.local",
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
        pass
    except json.JSONDecodeError as exc:
        failures.append("Coordinator runtime file")
        log("ERROR", f"Coordinator runtime file is invalid JSON: {exc}")

api_key = credentials.get("apiKey") or credentials.get("api_key")
assistant_id = credentials.get("coordinatorAgentId") or credentials.get("coordinator_agent_id")
if api_key and assistant_id:
    auth_headers = {"apiKey": str(api_key)}
    check(
        "Integration catalog",
        "GET",
        (
            f"{console}/api/integrations/provider/apps"
            f"?owner_scope=assistant&assistant_id={assistant_id}&limit=100&offset=0&detail_level=summary"
        ),
        {200},
        headers=auth_headers,
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
    )
else:
    log("INFO", "Coordinator checks skipped: register or sign in first.")

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
  # Console's local.sh owns the per-service logfiles for the stack
  # (console|orchestra|pubsub|stripe); delegate so there's one log surface.
  bash "$CONSOLE_LOCAL_SCRIPT" logs "$@"
}

main() {
  local cmd="${1:-up}"
  shift || true
  case "$cmd" in
    up) cmd_up "$@" ;;
    down|stop) cmd_down "$@" ;;
    status) cmd_status "$@" ;;
    logs) cmd_logs "$@" ;;
    smoke) cmd_smoke "$@" ;;
    repair-console|restart-console) cmd_repair_console "$@" ;;
    dev-env|print-console-env) cmd_dev_env "$@" ;;
    doctor|check) cmd_doctor "$@" ;;
    help|-h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      ;;
    *)
      log_error "Unknown command: $cmd"
      echo "Run: $0 help"
      return 1
      ;;
  esac
}

main "$@"
