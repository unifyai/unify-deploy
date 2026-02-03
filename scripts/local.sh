#!/usr/bin/env bash
# =============================================================================
# local.sh - Manage local Communication services for development/testing
# =============================================================================
#
# This script starts a local Communication deployment with:
#   1. Google Cloud Pub/Sub Emulator (optional, for fully local testing)
#   2. Adapters FastAPI server (webhook handlers)
#   3. Communication FastAPI server (optional, for outbound APIs)
#
# This enables local Unity integration tests without requiring cloud Pub/Sub.
#
# Usage:
#   ./local.sh start              # Start with Pub/Sub emulator
#   ./local.sh start --no-emulator # Start without emulator (use real Pub/Sub)
#   ./local.sh stop               # Stop all services
#   ./local.sh restart            # Stop then start
#   ./local.sh check              # Check if running (returns URL or exits 1)
#   ./local.sh status             # Show detailed status
#   ./local.sh create-topics      # Create test topics in emulator
#
# Environment:
#   COMMS_REPO_PATH          Path to communication repo (default: auto-detect)
#   ADAPTERS_PORT            Adapters service port (default: 8081)
#   COMMS_PORT               Communication service port (default: 8082)
#   PUBSUB_EMULATOR_PORT     Pub/Sub emulator port (default: 8085)
#   ORCHESTRA_URL           Orchestra URL (if set, used instead of default)
#   GCP_PROJECT_ID           GCP project ID for Pub/Sub (default: local-test-project)
#
# Test Assistant:
#   Creates topics/subscriptions for 'default-test-assistant' on start.
#   Set TEST_ASSISTANT_ID to use a different assistant ID.
#
# On success, exports:
#   UNITY_ADAPTERS_URL=http://127.0.0.1:8081
#   UNITY_COMMS_URL=http://127.0.0.1:8082  (if communication service started)
#   PUBSUB_EMULATOR_HOST=localhost:8085    (if emulator started)
#
set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

# Resolve script directory and repo path
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
COMMS_REPO_PATH="${COMMS_REPO_PATH:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"

# Ports
ADAPTERS_PORT="${ADAPTERS_PORT:-8081}"
COMMS_PORT="${COMMS_PORT:-8082}"
PUBSUB_EMULATOR_PORT="${PUBSUB_EMULATOR_PORT:-8085}"

# Project ID for Pub/Sub (can be anything for emulator)
GCP_PROJECT_ID="${GCP_PROJECT_ID:-local-test-project}"

# Test assistant ID
TEST_ASSISTANT_ID="${TEST_ASSISTANT_ID:-default-test-assistant}"

# Staging suffix for topics
STAGING="${STAGING:-true}"
TOPIC_SUFFIX=""
if [[ "$STAGING" == "true" ]]; then
  TOPIC_SUFFIX="-staging"
fi

# PID and log files
COMMS_PREFIX="${COMMS_PREFIX:-communication}"
ADAPTERS_PIDFILE="/tmp/${COMMS_PREFIX}-adapters.pid"
ADAPTERS_LOGFILE="/tmp/${COMMS_PREFIX}-adapters.log"
COMMS_PIDFILE="/tmp/${COMMS_PREFIX}-comms.pid"
COMMS_LOGFILE="/tmp/${COMMS_PREFIX}-comms.log"
EMULATOR_PIDFILE="/tmp/${COMMS_PREFIX}-pubsub-emulator.pid"
EMULATOR_LOGFILE="/tmp/${COMMS_PREFIX}-pubsub-emulator.log"
CONFIG_FILE="/tmp/${COMMS_PREFIX}-local.config"

# URLs
LOCAL_ADAPTERS_URL="http://127.0.0.1:${ADAPTERS_PORT}"
LOCAL_COMMS_URL="http://127.0.0.1:${COMMS_PORT}"
LOCAL_PUBSUB_HOST="localhost:${PUBSUB_EMULATOR_PORT}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# =============================================================================
# Prerequisite Checks
# =============================================================================

check_gcloud() {
  if ! command -v gcloud &>/dev/null; then
    log_error "gcloud CLI is not installed"
    log_info "Install from: https://cloud.google.com/sdk/docs/install"
    return 1
  fi
  log_success "gcloud CLI is available"
  return 0
}

check_java() {
  # Pub/Sub emulator requires Java 7+
  if ! command -v java &>/dev/null; then
    log_error "Java is required for Pub/Sub emulator but not installed"
    log_info "Install Java with one of:"
    log_info "  macOS:  brew install openjdk"
    log_info "  Ubuntu: sudo apt install default-jdk"
    log_info "  Or use: $0 start --no-emulator"
    return 1
  fi
  log_success "Java is available"
  return 0
}

check_pubsub_emulator() {
  # First check Java (required for emulator)
  if ! check_java; then
    return 1
  fi

  # Check if pubsub-emulator component is installed
  if ! gcloud components list 2>/dev/null | grep -q "pubsub-emulator.*Installed"; then
    log_warn "Pub/Sub emulator not installed"
    log_info "Installing with: gcloud components install pubsub-emulator"
    if ! gcloud components install pubsub-emulator --quiet; then
      log_error "Failed to install Pub/Sub emulator"
      return 1
    fi
  fi

  # Check if beta commands are installed (needed for emulators command)
  if ! gcloud components list 2>/dev/null | grep -q "gcloud Beta Commands.*Installed"; then
    log_warn "gcloud beta commands not installed"
    log_info "Installing with: gcloud components install beta"
    if ! gcloud components install beta --quiet; then
      log_error "Failed to install gcloud beta commands"
      return 1
    fi
  fi

  log_success "Pub/Sub emulator is available"
  return 0
}

check_python() {
  if ! command -v python3 &>/dev/null; then
    log_error "Python 3 is not installed"
    return 1
  fi
  log_success "Python 3 is available"
  return 0
}

check_repo() {
  if [[ ! -d "$COMMS_REPO_PATH" ]]; then
    log_error "Communication repo not found at: $COMMS_REPO_PATH"
    return 1
  fi

  if [[ ! -f "$COMMS_REPO_PATH/pyproject.toml" ]]; then
    log_error "Communication repo appears incomplete (no pyproject.toml)"
    return 1
  fi

  log_success "Communication repo found at: $COMMS_REPO_PATH"
  return 0
}

# Get Python executable from venv or fallback
get_python() {
  local venv_python="$COMMS_REPO_PATH/.venv/bin/python"
  if [[ -x "$venv_python" ]]; then
    echo "$venv_python"
  else
    echo "python3"
  fi
}

# =============================================================================
# Pub/Sub Emulator Management
# =============================================================================

is_emulator_running() {
  if [[ -f "$EMULATOR_PIDFILE" ]]; then
    local pid
    pid=$(cat "$EMULATOR_PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  # Also check by port
  if lsof -i ":${PUBSUB_EMULATOR_PORT}" -sTCP:LISTEN &>/dev/null; then
    return 0
  fi
  return 1
}

start_pubsub_emulator() {
  log_info "Starting Pub/Sub emulator on port $PUBSUB_EMULATOR_PORT..."

  if is_emulator_running; then
    log_success "Pub/Sub emulator already running"
    return 0
  fi

  # Start emulator in background
  gcloud beta emulators pubsub start \
    --project="$GCP_PROJECT_ID" \
    --host-port="localhost:${PUBSUB_EMULATOR_PORT}" \
    > "$EMULATOR_LOGFILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$EMULATOR_PIDFILE"

  # Wait for emulator to be ready
  log_info "Waiting for Pub/Sub emulator to be ready..."
  local max_attempts=30
  local attempt=0
  while (( attempt < max_attempts )); do
    if curl -s "http://localhost:${PUBSUB_EMULATOR_PORT}" &>/dev/null; then
      log_success "Pub/Sub emulator is ready"
      return 0
    fi
    sleep 1
    ((attempt++)) || true
  done

  log_error "Pub/Sub emulator failed to start within 30 seconds"
  log_info "Check logs at: $EMULATOR_LOGFILE"
  return 1
}

stop_pubsub_emulator() {
  if [[ -f "$EMULATOR_PIDFILE" ]]; then
    local pid
    pid=$(cat "$EMULATOR_PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      log_info "Stopping Pub/Sub emulator (PID $pid)..."
      kill "$pid" 2>/dev/null || true
      sleep 2
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$EMULATOR_PIDFILE"
  fi

  # Also kill any processes on the emulator port
  local port_pids
  port_pids=$(lsof -t -i ":${PUBSUB_EMULATOR_PORT}" 2>/dev/null || true)
  if [[ -n "$port_pids" ]]; then
    echo "$port_pids" | xargs kill -9 2>/dev/null || true
  fi

  log_success "Pub/Sub emulator stopped"
}

create_pubsub_topics() {
  # Create topics and subscriptions for the test assistant
  # This uses the emulator's REST API directly

  local base_url="http://localhost:${PUBSUB_EMULATOR_PORT}/v1"

  log_info "Creating Pub/Sub topics for test assistant: $TEST_ASSISTANT_ID"

  # Topics to create
  local topics=(
    "unity-${TEST_ASSISTANT_ID}${TOPIC_SUFFIX}"
    "unity-startup${TOPIC_SUFFIX}"
  )

  for topic in "${topics[@]}"; do
    local topic_path="projects/${GCP_PROJECT_ID}/topics/${topic}"
    local sub_path="projects/${GCP_PROJECT_ID}/subscriptions/${topic}-sub"

    # Create topic
    if curl -s -X PUT "${base_url}/${topic_path}" -H "Content-Type: application/json" -d '{}' &>/dev/null; then
      log_success "Created topic: $topic"
    else
      log_warn "Topic may already exist: $topic"
    fi

    # Create subscription
    local sub_body="{\"topic\": \"${topic_path}\"}"
    if curl -s -X PUT "${base_url}/${sub_path}" -H "Content-Type: application/json" -d "$sub_body" &>/dev/null; then
      log_success "Created subscription: ${topic}-sub"
    else
      log_warn "Subscription may already exist: ${topic}-sub"
    fi
  done
}

# =============================================================================
# Adapters Service Management
# =============================================================================

is_adapters_running() {
  if [[ -f "$ADAPTERS_PIDFILE" ]]; then
    local pid
    pid=$(cat "$ADAPTERS_PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

start_adapters_service() {
  log_info "Starting Adapters service on port $ADAPTERS_PORT..."

  if is_adapters_running; then
    log_success "Adapters service already running"
    return 0
  fi

  # Check if port is in use
  if lsof -i ":${ADAPTERS_PORT}" -sTCP:LISTEN &>/dev/null; then
    log_error "Port $ADAPTERS_PORT is already in use"
    return 1
  fi

  cd "$COMMS_REPO_PATH"

  local python_cmd
  python_cmd=$(get_python)

  # Set environment for the service
  local env_vars=(
    "GCP_PROJECT_ID=$GCP_PROJECT_ID"
    "STAGING=$STAGING"
    "UNITY_ADAPTERS_URL=$LOCAL_ADAPTERS_URL"
  )

  # Add PUBSUB_EMULATOR_HOST if emulator is running
  if is_emulator_running; then
    env_vars+=("PUBSUB_EMULATOR_HOST=$LOCAL_PUBSUB_HOST")
  fi

  # Add ORCHESTRA_ADMIN_KEY if set (required for auth)
  if [[ -n "${ORCHESTRA_ADMIN_KEY:-}" ]]; then
    env_vars+=("ORCHESTRA_ADMIN_KEY=$ORCHESTRA_ADMIN_KEY")
  fi

  # Add ORCHESTRA_URL if set (for local orchestra)
  if [[ -n "${ORCHESTRA_URL:-}" ]]; then
    env_vars+=("ORCHESTRA_URL=$ORCHESTRA_URL")
  fi

  # Start the service
  env "${env_vars[@]}" $python_cmd -m uvicorn adapters.main:app \
    --host 0.0.0.0 \
    --port "$ADAPTERS_PORT" \
    > "$ADAPTERS_LOGFILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$ADAPTERS_PIDFILE"

  # Wait for service to be ready
  log_info "Waiting for Adapters service to be ready..."
  local max_attempts=30
  local attempt=0
  while (( attempt < max_attempts )); do
    if curl -s "${LOCAL_ADAPTERS_URL}/health" &>/dev/null; then
      log_success "Adapters service is ready at $LOCAL_ADAPTERS_URL"
      return 0
    fi
    sleep 1
    ((attempt++)) || true
  done

  log_error "Adapters service failed to start within 30 seconds"
  log_info "Check logs at: $ADAPTERS_LOGFILE"
  return 1
}

stop_adapters_service() {
  if [[ -f "$ADAPTERS_PIDFILE" ]]; then
    local pid
    pid=$(cat "$ADAPTERS_PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      log_info "Stopping Adapters service (PID $pid)..."
      kill "$pid" 2>/dev/null || true
      sleep 2
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$ADAPTERS_PIDFILE"
  fi
  log_success "Adapters service stopped"
}

# =============================================================================
# Communication Service Management (Optional)
# =============================================================================

is_comms_running() {
  if [[ -f "$COMMS_PIDFILE" ]]; then
    local pid
    pid=$(cat "$COMMS_PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

start_comms_service() {
  log_info "Starting Communication service on port $COMMS_PORT..."

  if is_comms_running; then
    log_success "Communication service already running"
    return 0
  fi

  # Check if port is in use
  if lsof -i ":${COMMS_PORT}" -sTCP:LISTEN &>/dev/null; then
    log_error "Port $COMMS_PORT is already in use"
    return 1
  fi

  cd "$COMMS_REPO_PATH"

  local python_cmd
  python_cmd=$(get_python)

  # Set environment for the service
  local env_vars=(
    "GCP_PROJECT_ID=$GCP_PROJECT_ID"
    "STAGING=$STAGING"
    "UNITY_COMMS_URL=$LOCAL_COMMS_URL"
  )

  # Add ORCHESTRA_ADMIN_KEY if set
  if [[ -n "${ORCHESTRA_ADMIN_KEY:-}" ]]; then
    env_vars+=("ORCHESTRA_ADMIN_KEY=$ORCHESTRA_ADMIN_KEY")
  fi

  # Start the service
  env "${env_vars[@]}" $python_cmd -m uvicorn communication.main:app \
    --host 0.0.0.0 \
    --port "$COMMS_PORT" \
    > "$COMMS_LOGFILE" 2>&1 &
  local pid=$!
  echo "$pid" > "$COMMS_PIDFILE"

  # Wait for service to be ready
  log_info "Waiting for Communication service to be ready..."
  local max_attempts=30
  local attempt=0
  while (( attempt < max_attempts )); do
    if curl -s "${LOCAL_COMMS_URL}/" &>/dev/null; then
      log_success "Communication service is ready at $LOCAL_COMMS_URL"
      return 0
    fi
    sleep 1
    ((attempt++)) || true
  done

  log_error "Communication service failed to start within 30 seconds"
  log_info "Check logs at: $COMMS_LOGFILE"
  return 1
}

stop_comms_service() {
  if [[ -f "$COMMS_PIDFILE" ]]; then
    local pid
    pid=$(cat "$COMMS_PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      log_info "Stopping Communication service (PID $pid)..."
      kill "$pid" 2>/dev/null || true
      sleep 2
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$COMMS_PIDFILE"
  fi
  log_success "Communication service stopped"
}

# =============================================================================
# Main Commands
# =============================================================================

cmd_start() {
  local use_emulator=true
  local start_comms=false

  # Parse arguments
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-emulator)
        use_emulator=false
        shift
        ;;
      --with-comms)
        start_comms=true
        shift
        ;;
      *)
        shift
        ;;
    esac
  done

  echo "=============================================="
  echo "Starting Local Communication Services"
  echo "=============================================="
  echo ""

  # Check prerequisites
  if ! check_python; then
    return 1
  fi

  if ! check_repo; then
    return 1
  fi

  if [[ "$use_emulator" == "true" ]]; then
    if ! check_gcloud; then
      log_warn "gcloud not available, skipping Pub/Sub emulator"
      use_emulator=false
    elif ! check_pubsub_emulator; then
      log_warn "Could not install Pub/Sub emulator, using real Pub/Sub"
      use_emulator=false
    fi
  fi

  echo ""

  # Start Pub/Sub emulator if requested
  if [[ "$use_emulator" == "true" ]]; then
    if ! start_pubsub_emulator; then
      log_error "Failed to start Pub/Sub emulator"
      return 1
    fi

    # Create topics for test assistant
    create_pubsub_topics
    echo ""
  fi

  # Start Adapters service
  if ! start_adapters_service; then
    log_error "Failed to start Adapters service"
    return 1
  fi

  # Optionally start Communication service
  if [[ "$start_comms" == "true" ]]; then
    if ! start_comms_service; then
      log_warn "Failed to start Communication service (continuing anyway)"
    fi
  fi

  # Write config file for external tools
  {
    echo "ADAPTERS_PORT=$ADAPTERS_PORT"
    echo "UNITY_ADAPTERS_URL=$LOCAL_ADAPTERS_URL"
    if [[ "$use_emulator" == "true" ]]; then
      echo "PUBSUB_EMULATOR_HOST=$LOCAL_PUBSUB_HOST"
      echo "PUBSUB_EMULATOR_PORT=$PUBSUB_EMULATOR_PORT"
    fi
    if [[ "$start_comms" == "true" ]]; then
      echo "COMMS_PORT=$COMMS_PORT"
      echo "UNITY_COMMS_URL=$LOCAL_COMMS_URL"
    fi
    echo "GCP_PROJECT_ID=$GCP_PROJECT_ID"
    echo "TEST_ASSISTANT_ID=$TEST_ASSISTANT_ID"
  } > "$CONFIG_FILE"

  echo ""
  echo "=============================================="
  log_success "Local Communication services are ready!"
  echo "=============================================="
  echo ""
  echo "Adapters URL:   $LOCAL_ADAPTERS_URL"
  if [[ "$use_emulator" == "true" ]]; then
    echo "Pub/Sub Host:   $LOCAL_PUBSUB_HOST"
  fi
  if [[ "$start_comms" == "true" ]]; then
    echo "Comms URL:      $LOCAL_COMMS_URL"
  fi
  echo ""
  echo "To use in your shell:"
  echo "  export UNITY_ADAPTERS_URL='$LOCAL_ADAPTERS_URL'"
  if [[ "$use_emulator" == "true" ]]; then
    echo "  export PUBSUB_EMULATOR_HOST='$LOCAL_PUBSUB_HOST'"
  fi
  if [[ "$start_comms" == "true" ]]; then
    echo "  export UNITY_COMMS_URL='$LOCAL_COMMS_URL'"
  fi
  echo ""
  echo "Test assistant topics created:"
  echo "  unity-${TEST_ASSISTANT_ID}${TOPIC_SUFFIX}"
  echo "  unity-startup${TOPIC_SUFFIX}"
  echo ""

  # Output for eval
  echo "export UNITY_ADAPTERS_URL='$LOCAL_ADAPTERS_URL'"
  if [[ "$use_emulator" == "true" ]]; then
    echo "export PUBSUB_EMULATOR_HOST='$LOCAL_PUBSUB_HOST'"
  fi
  if [[ "$start_comms" == "true" ]]; then
    echo "export UNITY_COMMS_URL='$LOCAL_COMMS_URL'"
  fi

  return 0
}

cmd_stop() {
  echo "Stopping Local Communication services..."
  echo ""

  stop_adapters_service
  stop_comms_service
  stop_pubsub_emulator

  rm -f "$CONFIG_FILE"

  echo ""
  log_success "Local Communication services stopped"
}

cmd_restart() {
  cmd_stop
  echo ""
  cmd_start "$@"
}

cmd_status() {
  echo "Local Communication Status"
  echo "=========================="
  echo ""

  echo -n "Pub/Sub Emulator: "
  if is_emulator_running; then
    echo -e "${GREEN}running (port $PUBSUB_EMULATOR_PORT)${NC}"
  else
    echo -e "${RED}not running${NC}"
  fi

  echo -n "Adapters Service: "
  if is_adapters_running; then
    echo -e "${GREEN}running (port $ADAPTERS_PORT)${NC}"
  else
    echo -e "${RED}not running${NC}"
  fi

  echo -n "Communication Service: "
  if is_comms_running; then
    echo -e "${GREEN}running (port $COMMS_PORT)${NC}"
  else
    echo -e "${RED}not running${NC}"
  fi

  echo ""
  echo "Configuration:"
  echo "  Repo Path:        $COMMS_REPO_PATH"
  echo "  Adapters Port:    $ADAPTERS_PORT"
  echo "  Comms Port:       $COMMS_PORT"
  echo "  Emulator Port:    $PUBSUB_EMULATOR_PORT"
  echo "  GCP Project ID:       $GCP_PROJECT_ID"
  echo "  Test Assistant:   $TEST_ASSISTANT_ID"
  echo ""

  if [[ -f "$CONFIG_FILE" ]]; then
    echo "Config file: $CONFIG_FILE"
    cat "$CONFIG_FILE"
  fi
}

cmd_check() {
  # Check if adapters is running and return URL
  if is_adapters_running; then
    if curl -s "${LOCAL_ADAPTERS_URL}/health" &>/dev/null; then
      echo "$LOCAL_ADAPTERS_URL"
      return 0
    fi
  fi
  return 1
}

cmd_create_topics() {
  if ! is_emulator_running; then
    log_error "Pub/Sub emulator is not running"
    log_info "Start it with: $0 start"
    return 1
  fi
  create_pubsub_topics
}

cmd_help() {
  echo "Usage: $0 [command] [options]"
  echo ""
  echo "Commands:"
  echo "  start              Start services (with Pub/Sub emulator by default)"
  echo "  stop               Stop all services"
  echo "  restart            Stop then start"
  echo "  status             Show detailed status"
  echo "  check              Quick check if running (returns URL or exits 1)"
  echo "  create-topics      Create test topics in emulator"
  echo ""
  echo "Start Options:"
  echo "  --no-emulator      Don't start Pub/Sub emulator (use real Pub/Sub)"
  echo "  --with-comms       Also start Communication service (for outbound)"
  echo ""
  echo "Environment Variables:"
  echo "  COMMS_REPO_PATH        Path to communication repo (default: auto-detect)"
  echo "  ADAPTERS_PORT          Adapters service port (default: 8081)"
  echo "  COMMS_PORT             Communication service port (default: 8082)"
  echo "  PUBSUB_EMULATOR_PORT   Pub/Sub emulator port (default: 8085)"
  echo "  GCP_PROJECT_ID                   GCP project ID (default: local-test-project)"
  echo "  TEST_ASSISTANT_ID      Test assistant ID (default: default-test-assistant)"
  echo "  ORCHESTRA_URL         Orchestra URL (if using local orchestra)"
  echo "  ORCHESTRA_ADMIN_KEY    Admin key for Orchestra auth"
  echo "  STAGING                Set to 'true' for staging topics (default: true)"
  echo ""
  echo "Examples:"
  echo "  $0 start                              # Start with emulator"
  echo "  $0 start --no-emulator                # Start without emulator"
  echo "  $0 start --with-comms                 # Start both services"
  echo "  eval \"\$($0 start)\"                   # Start and set env vars"
  echo ""
}

# =============================================================================
# Entry Point
# =============================================================================

main() {
  local cmd="${1:-help}"
  shift || true

  case "$cmd" in
    start)
      cmd_start "$@"
      ;;
    stop)
      cmd_stop
      ;;
    restart)
      cmd_restart "$@"
      ;;
    status)
      cmd_status
      ;;
    check)
      cmd_check
      ;;
    create-topics)
      cmd_create_topics
      ;;
    help|--help|-h)
      cmd_help
      ;;
    *)
      log_error "Unknown command: $cmd"
      echo "Run '$0 help' for usage"
      exit 1
      ;;
  esac
}

main "$@"
