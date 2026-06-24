#!/usr/bin/env bash
# =============================================================================
# service.sh — Self-host background runtime (Orchestra + Coordinator CM)
# =============================================================================
#
# The runtime service keeps Orchestra, droid.gateway, and one Coordinator CM
# alive for scheduled tasks and outbound comms primitives. Self-host enables
# this automatically on setup and stack up. The interactive stack (droid stack
# up/down) starts Console, Pub/Sub, and droid.gateway without replacing
# the service-managed gateway or CM.
#
# Usage:
#   ./scripts/service.sh install-boot [--boot]  Optional login boot hook (launchd/systemd)
#   ./scripts/service.sh install [--boot]       Alias for install-boot
#   ./scripts/service.sh uninstall              Remove boot hook + disable runtime
#   ./scripts/service.sh disable                Stop runtime and opt out until next stack up
#   ./scripts/service.sh start                  Recovery: start supervisor manually
#   ./scripts/service.sh stop                   Stop supervisor + service-owned runtime
#   ./scripts/service.sh status                 Show supervisor + CM status
#   ./scripts/service.sh doctor                 Health checks for runtime service
#   ./scripts/service.sh run                    Foreground supervisor (used by OS unit)
#
set -euo pipefail

# This script lives in droid-deploy/selfhost/ and drives the sibling droid,
# console, and orchestra checkouts under UNIFY_STACK_ROOT.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEPLOY_REPO_PATH="$(cd "$SCRIPT_DIR/.." && pwd -P)"
SELF_HOST_ENV_SCRIPT="$SCRIPT_DIR/self_host_env.sh"

UNIFY_STACK_ROOT="${UNIFY_STACK_ROOT:-$(cd "$DEPLOY_REPO_PATH/.." && pwd -P)}"
DROID_REPO_PATH="${DROID_REPO_PATH:-$UNIFY_STACK_ROOT/droid}"
CONSOLE_REPO_PATH="${CONSOLE_REPO_PATH:-$UNIFY_STACK_ROOT/console}"
ORCHESTRA_REPO_PATH="${ORCHESTRA_REPO_PATH:-$UNIFY_STACK_ROOT/orchestra}"

CONSOLE_LOCAL_SCRIPT="$CONSOLE_REPO_PATH/scripts/local.sh"
SERVICE_LABEL="${DROID_SERVICE_LABEL:-ai.unify.droid.runtime}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*"; }

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

load_self_host_context() {
  export SELF_HOST=1
  export DROID_REPO_PATH
  export CONSOLE_REPO_PATH
  export ORCHESTRA_REPO_PATH
  export ORCHESTRA_DB_PORT="$(default_orchestra_db_port)"
  export DROID_HOME="${DROID_HOME:-$HOME/.droid}"
  if [[ -f "$SELF_HOST_ENV_SCRIPT" ]]; then
    # shellcheck disable=SC1090
    source "$SELF_HOST_ENV_SCRIPT"
    export_self_host_coordinator_runtime_file
    load_self_host_env_file "$DROID_REPO_PATH/.env"
    # Hosted Coordinator comms (internal dev): load the comms SA (Gmail) and
    # Twilio creds/numbers so the gateway this supervisor launches can send as
    # the Coordinator and the comms bridge can poll inbound. No-op when absent.
    if declare -F self_host_export_comms_sa &>/dev/null; then
      self_host_export_comms_sa
    fi
    if declare -F self_host_export_comms_twilio &>/dev/null; then
      self_host_export_comms_twilio
    fi
    # Self-host always runs with Console, so the Coordinator onboarding flow
    # (narration + reference quiz) must stay active. The public droid default
    # (droid/.env) disables it for headless installs; force it on here.
    export DROID_CONSOLE_UI=true
    # The self-host CM is the personal Coordinator, so surface its universal
    # email (and provider) the way the hosted assignment event would. Without
    # this the CM boots with no email and outbound Coordinator mail / the
    # reference quiz fail. No-op until a Coordinator mailbox is configured.
    if [[ -n "${DROID_COORDINATOR_EMAIL_ADDRESS:-}" ]]; then
      export ASSISTANT_EMAIL="${DROID_COORDINATOR_EMAIL_ADDRESS}"
      export ASSISTANT_EMAIL_PROVIDER="${ASSISTANT_EMAIL_PROVIDER:-google_workspace}"
    fi
  fi
}

launchd_plist_path() {
  printf '%s/Library/LaunchAgents/%s.plist' "$HOME" "$SERVICE_LABEL"
}

systemd_unit_path() {
  printf '%s/.config/systemd/user/%s.service' "$HOME" "$SERVICE_LABEL"
}

service_install_launchd() {
  local boot_at_load="$1"
  local plist_path
  plist_path="$(launchd_plist_path)"
  mkdir -p "$(dirname "$plist_path")"
  local run_at_load_xml="<false/>"
  [[ "$boot_at_load" == "true" ]] && run_at_load_xml="<true/>"

  cat >"$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${SERVICE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${SCRIPT_DIR}/service.sh</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key>
  ${run_at_load_xml}
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$(self_host_service_log_file)</string>
  <key>StandardErrorPath</key>
  <string>$(self_host_service_log_file)</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SELF_HOST</key>
    <string>1</string>
    <key>DROID_HOME</key>
    <string>${DROID_HOME:-$HOME/.droid}</string>
    <key>DROID_REPO_PATH</key>
    <string>${DROID_REPO_PATH}</string>
    <key>CONSOLE_REPO_PATH</key>
    <string>${CONSOLE_REPO_PATH}</string>
    <key>ORCHESTRA_REPO_PATH</key>
    <string>${ORCHESTRA_REPO_PATH}</string>
    <key>ORCHESTRA_DB_PORT</key>
    <string>${ORCHESTRA_DB_PORT:-55432}</string>
  </dict>
</dict>
</plist>
EOF

  launchctl bootout "gui/$(id -u)/$SERVICE_LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist_path"
  launchctl enable "gui/$(id -u)/$SERVICE_LABEL" 2>/dev/null || true
  if [[ "$boot_at_load" == "true" ]]; then
    launchctl kickstart -k "gui/$(id -u)/$SERVICE_LABEL" 2>/dev/null \
      || launchctl start "$SERVICE_LABEL" 2>/dev/null \
      || true
  fi
}

service_install_systemd() {
  local boot_at_load="$1"
  local unit_path
  unit_path="$(systemd_unit_path)"
  mkdir -p "$(dirname "$unit_path")"

  cat >"$unit_path" <<EOF
[Unit]
Description=Unify self-host runtime (Orchestra + gateway + Coordinator CM)
After=network.target

[Service]
Type=simple
ExecStart=/bin/bash ${SCRIPT_DIR}/service.sh run
Restart=always
RestartSec=5
Environment=SELF_HOST=1
Environment=DROID_HOME=${DROID_HOME:-$HOME/.droid}
Environment=DROID_REPO_PATH=${DROID_REPO_PATH}
Environment=CONSOLE_REPO_PATH=${CONSOLE_REPO_PATH}
Environment=ORCHESTRA_REPO_PATH=${ORCHESTRA_REPO_PATH}
Environment=ORCHESTRA_DB_PORT=${ORCHESTRA_DB_PORT:-55432}

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE_LABEL"
  if [[ "$boot_at_load" == "true" ]]; then
    systemctl --user start "$SERVICE_LABEL"
  fi
}

service_uninstall_launchd() {
  local plist_path
  plist_path="$(launchd_plist_path)"
  launchctl bootout "gui/$(id -u)/$SERVICE_LABEL" 2>/dev/null || true
  rm -f "$plist_path"
}

service_uninstall_systemd() {
  systemctl --user stop "$SERVICE_LABEL" 2>/dev/null || true
  systemctl --user disable "$SERVICE_LABEL" 2>/dev/null || true
  rm -f "$(systemd_unit_path)"
  systemctl --user daemon-reload
}

cmd_install_boot_hook() {
  local boot_at_load="false"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --boot) boot_at_load="true"; shift ;;
      *) log_error "Unknown option: $1"; return 1 ;;
    esac
  done

  load_self_host_context
  self_host_enable_runtime

  case "$(uname -s)" in
    Darwin)
      service_install_launchd "$boot_at_load"
      ;;
    Linux)
      if ! command -v systemctl &>/dev/null; then
        log_error "systemctl not found — cannot install systemd user service"
        return 1
      fi
      service_install_systemd "$boot_at_load"
      ;;
    *)
      log_error "Unsupported OS for boot hook install: $(uname -s)"
      return 1
      ;;
  esac

  log_success "Login boot hook installed"
  if [[ "$boot_at_load" == "true" ]]; then
    log_info "Background runtime starts automatically at login"
  else
    log_info "Supervisor starts on droid stack up; re-run with --boot to start at login"
  fi
}

cmd_install() {
  cmd_install_boot_hook "$@"
}

cmd_disable() {
  load_self_host_context
  cmd_stop || true
  self_host_disable_runtime
  log_success "Background runtime disabled"
  log_info "stack down stops everything until the next droid stack up"
}

cmd_uninstall() {
  load_self_host_context
  cmd_stop || true
  case "$(uname -s)" in
    Darwin) service_uninstall_launchd ;;
    Linux) service_uninstall_systemd ;;
  esac
  self_host_disable_runtime
  log_success "Background runtime disabled and boot hook removed"
}

ensure_runtime_backend() {
  if [[ ! -x "$CONSOLE_LOCAL_SCRIPT" ]]; then
    log_error "Missing $CONSOLE_LOCAL_SCRIPT"
    return 1
  fi
  export DROID_RUNTIME_OWNER="$SELF_HOST_RUNTIME_OWNER_SERVICE"
  export DROID_SERVICE_RUNTIME=1
  if declare -F with_droid_runtime_start_lock >/dev/null 2>&1; then
    with_droid_runtime_start_lock 30 bash "$CONSOLE_LOCAL_SCRIPT" start-runtime-backend
  else
    bash "$CONSOLE_LOCAL_SCRIPT" start-runtime-backend
  fi
}

cmd_run() {
  load_self_host_context
  self_host_ensure_state_dir
  echo $$ >"$(self_host_service_supervisor_pidfile)"

  log_info "Self-host runtime supervisor started (pid $$)"
  while true; do
    if ! ensure_runtime_backend; then
      log_warn "Runtime backend unhealthy — retrying in 15s"
      sleep 15
      continue
    fi
    # Keep the comms ingress bridge alive when hosted Coordinator comms are
    # configured (idempotent; restarts it if it died). No-op otherwise.
    if declare -F self_host_ensure_comms_bridge &>/dev/null; then
      self_host_ensure_comms_bridge || log_warn "Comms ingress bridge failed to start"
    fi
    sleep 30
    local count
    count="$(droid_cm_instance_count)"
    if [[ "$count" -eq 0 ]]; then
      log_warn "Coordinator CM exited — restarting runtime backend"
      continue
    fi
    if [[ "$count" -gt 1 ]]; then
      log_error "Multiple Coordinator CM processes detected ($count) — resetting runtime"
      if [[ -x "$CONSOLE_LOCAL_SCRIPT" ]]; then
        export DROID_ALLOW_RUNTIME_STOP=1
        bash "$CONSOLE_LOCAL_SCRIPT" stop-runtime-backend || true
      fi
      sleep 2
      continue
    fi
    if declare -F self_host_gateway_is_healthy &>/dev/null \
      && ! self_host_gateway_is_healthy; then
      log_warn "Service gateway unhealthy — restarting runtime backend"
      continue
    fi
  done
}

cmd_start() {
  load_self_host_context
  self_host_enable_runtime

  if self_host_service_supervisor_is_running; then
    log_success "Runtime supervisor already running (pid $(cat "$(self_host_service_supervisor_pidfile)"))"
    return 0
  fi

  local log_file
  log_file="$(self_host_service_log_file)"
  nohup bash "$SCRIPT_DIR/service.sh" run >>"$log_file" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  sleep 2
  if ! kill -0 "$pid" 2>/dev/null; then
    log_error "Runtime supervisor failed to start — see $log_file"
    tail -20 "$log_file" 2>/dev/null || true
    return 1
  fi
  log_success "Runtime supervisor started (pid $pid)"
  log_info "Logs: $log_file"
}

cmd_stop() {
  load_self_host_context

  case "$(uname -s)" in
    Darwin)
      if [[ -f "$(launchd_plist_path)" ]]; then
        launchctl bootout "gui/$(id -u)/$SERVICE_LABEL" 2>/dev/null || true
      fi
      ;;
    Linux)
      if [[ -f "$(systemd_unit_path)" ]]; then
        systemctl --user stop "$SERVICE_LABEL" 2>/dev/null || true
      fi
      ;;
  esac

  if self_host_service_supervisor_is_running; then
    local supervisor_pid
    supervisor_pid="$(cat "$(self_host_service_supervisor_pidfile)" 2>/dev/null || true)"
    if [[ -n "$supervisor_pid" ]]; then
      log_info "Stopping runtime supervisor (pid $supervisor_pid)..."
      kill "$supervisor_pid" 2>/dev/null || true
      sleep 2
      kill -9 "$supervisor_pid" 2>/dev/null || true
    fi
    self_host_clear_service_supervisor_pidfile
  else
    self_host_clear_service_supervisor_pidfile
  fi

  if declare -F self_host_stop_comms_bridge &>/dev/null; then
    self_host_stop_comms_bridge || true
  fi

  if [[ -x "$CONSOLE_LOCAL_SCRIPT" ]]; then
    export DROID_RUNTIME_OWNER="$SELF_HOST_RUNTIME_OWNER_SERVICE"
    bash "$CONSOLE_LOCAL_SCRIPT" stop-runtime-backend || true
  fi

  log_success "Self-host runtime service stopped"
  log_info "Scheduled tasks resume on the next droid stack up"
}

cmd_status() {
  load_self_host_context
  echo ""
  echo "Self-host runtime service"
  echo "======================="
  echo ""
  self_host_runtime_doctor_line | sed 's/^/  /'
  echo ""
  if self_host_service_supervisor_is_running; then
    echo "  Supervisor: running (pid $(cat "$(self_host_service_supervisor_pidfile)"))"
  else
    echo "  Supervisor: stopped"
  fi
  if [[ -f "$(self_host_service_log_file)" ]]; then
    echo "  Log: $(self_host_service_log_file)"
  fi
  echo ""
}

cmd_doctor() {
  local ok=true
  load_self_host_context
  echo ""
  echo "Self-host runtime doctor"
  echo "========================"
  echo ""

  if self_host_service_is_enabled; then
    log_success "Background runtime enabled"
  else
    log_info "Background runtime disabled — run droid stack up to re-enable"
  fi

  local cm_count
  cm_count="$(droid_cm_instance_count)"
  if [[ "$cm_count" -eq 0 ]]; then
    log_warn "Coordinator CM not running"
    ok=false
  elif [[ "$cm_count" -eq 1 ]]; then
    log_success "Coordinator CM: 1 instance"
  else
    log_error "Coordinator CM: $cm_count instances (split brain risk)"
    ok=false
  fi

  if self_host_service_is_enabled; then
    if self_host_service_supervisor_is_running; then
      log_success "Runtime supervisor running"
    else
      log_warn "Runtime supervisor stopped — run: droid stack up"
      ok=false
    fi
  fi

  if [[ -f "${SELF_HOST_COORDINATOR_RUNTIME_FILE:-}" ]]; then
    log_success "Coordinator credentials: ${SELF_HOST_COORDINATOR_RUNTIME_FILE}"
  else
    log_warn "No coordinator-runtime.json — register at Console first"
    ok=false
  fi

  if declare -F self_host_gateway_is_healthy &>/dev/null; then
    if self_host_gateway_is_healthy; then
      log_success "Droid gateway: $(self_host_gateway_base_url)"
    else
      log_warn "Droid gateway not healthy — outbound comms will fail until it restarts"
      ok=false
    fi
  fi

  echo ""
  if [[ "$ok" == "true" ]]; then
    log_success "Runtime service healthy"
    return 0
  fi
  log_error "Runtime service needs attention"
  return 1
}

main() {
  local cmd="${1:-help}"
  shift || true
  case "$cmd" in
    install-boot) cmd_install_boot_hook "$@" ;;
    install) cmd_install "$@" ;;
    uninstall) cmd_uninstall "$@" ;;
    disable) cmd_disable "$@" ;;
    start) cmd_start "$@" ;;
    stop) cmd_stop "$@" ;;
    status) cmd_status "$@" ;;
    doctor) cmd_doctor "$@" ;;
    run) cmd_run "$@" ;;
    help|-h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      ;;
    *)
      log_error "Unknown command: $cmd"
      echo "Run: $0 help"
      return 1
      ;;
  esac
}

main "$@"
