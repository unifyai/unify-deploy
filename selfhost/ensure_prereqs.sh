#!/usr/bin/env bash
# =============================================================================
# ensure_prereqs.sh — Shared self-host prerequisites with auto-install
# =============================================================================
#
# Used by unity stack doctor, setup, and console local.sh (self-host).
# Attempts to install missing Java and Pub/Sub emulator components before failing.
#
# Usage:
#   source /path/to/unity/scripts/ensure_prereqs.sh
#   ensure_java || return 1
#   ensure_pubsub_emulator || return 1
#
set -euo pipefail

ENSURE_PREREQS_AUTO_INSTALL="${ENSURE_PREREQS_AUTO_INSTALL:-1}"

_ensure_prereqs_log_info() {
  if declare -F log_info &>/dev/null; then
    log_info "$1"
  else
    echo "→ $1"
  fi
}

_ensure_prereqs_log_success() {
  if declare -F log_success &>/dev/null; then
    log_success "$1"
  else
    echo "✓ $1"
  fi
}

_ensure_prereqs_log_warn() {
  if declare -F log_warn &>/dev/null; then
    log_warn "$1"
  else
    echo "⚠ $1"
  fi
}

_ensure_prereqs_log_error() {
  if declare -F log_error &>/dev/null; then
    log_error "$1"
  else
    echo "✗ $1" >&2
  fi
}

_java_works() {
  command -v java &>/dev/null && java -version &>/dev/null 2>&1
}

_activate_brew_openjdk() {
  if ! command -v brew &>/dev/null; then
    return 1
  fi
  local openjdk_prefix
  openjdk_prefix="$(brew --prefix openjdk 2>/dev/null || true)"
  if [[ -n "$openjdk_prefix" && -x "$openjdk_prefix/bin/java" ]]; then
    export JAVA_HOME="$openjdk_prefix"
    export PATH="$openjdk_prefix/bin:$PATH"
    _java_works && return 0
  fi
  if [[ -n "$openjdk_prefix" && -x "$openjdk_prefix/libexec/openjdk.jdk/Contents/Home/bin/java" ]]; then
    export JAVA_HOME="$openjdk_prefix/libexec/openjdk.jdk/Contents/Home"
    export PATH="$JAVA_HOME/bin:$PATH"
    _java_works && return 0
  fi
  return 1
}

_try_install_java() {
  if [[ "$ENSURE_PREREQS_AUTO_INSTALL" != "1" ]]; then
    return 1
  fi
  case "$(uname -s)" in
    Darwin)
      if command -v brew &>/dev/null; then
        _ensure_prereqs_log_info "Installing OpenJDK via Homebrew..."
        if brew install openjdk &>/dev/null; then
          _activate_brew_openjdk && return 0
        fi
      fi
      ;;
    Linux)
      if command -v apt-get &>/dev/null; then
        _ensure_prereqs_log_info "Installing default-jdk via apt..."
        if sudo apt-get install -y default-jdk &>/dev/null; then
          _java_works && return 0
        fi
      fi
      ;;
  esac
  return 1
}

ensure_java() {
  if _java_works; then
    return 0
  fi

  if _activate_brew_openjdk; then
    _ensure_prereqs_log_success "Java found (Homebrew OpenJDK)"
    return 0
  fi

  if _try_install_java; then
    _ensure_prereqs_log_success "Java installed"
    return 0
  fi

  _ensure_prereqs_log_error "Java JRE is required for the Pub/Sub emulator"
  _ensure_prereqs_log_info "macOS:  brew install openjdk"
  _ensure_prereqs_log_info "Ubuntu: sudo apt install default-jdk"
  return 1
}

_pubsub_emulator_installed() {
  local sdk_root
  sdk_root="$(gcloud info --format='value(installation.sdk_root)' 2>/dev/null || echo "")"
  local candidate
  for candidate in \
    "${sdk_root:+$sdk_root/platform/pubsub-emulator}" \
    "/usr/lib/google-cloud-sdk/platform/pubsub-emulator" \
    "$HOME/google-cloud-sdk/platform/pubsub-emulator"; do
    if [[ -n "$candidate" && -d "$candidate" ]]; then
      return 0
    fi
  done
  return 1
}

_gcloud_works() {
  command -v gcloud &>/dev/null && gcloud --version &>/dev/null 2>&1
}

_activate_gcloud_sdk() {
  local candidate
  for candidate in \
    "$HOME/google-cloud-sdk/bin/gcloud" \
    "/usr/local/google-cloud-sdk/bin/gcloud" \
    "/usr/lib/google-cloud-sdk/bin/gcloud"; do
    if [[ -x "$candidate" ]]; then
      export PATH="$(dirname "$candidate"):$PATH"
      _gcloud_works && return 0
    fi
  done
  return 1
}

_try_install_gcloud() {
  if [[ "$ENSURE_PREREQS_AUTO_INSTALL" != "1" ]]; then
    return 1
  fi
  if _activate_gcloud_sdk; then
    return 0
  fi
  _ensure_prereqs_log_info "Installing Google Cloud SDK (required for Pub/Sub emulator)..."
  if curl -fsSL https://sdk.cloud.google.com | bash -s -- \
    --disable-prompts \
    --install-dir="$HOME/google-cloud-sdk" >/dev/null 2>&1; then
    export PATH="$HOME/google-cloud-sdk/bin:$PATH"
    _gcloud_works && return 0
  fi
  return 1
}

ensure_gcloud() {
  if _gcloud_works; then
    return 0
  fi
  if _activate_gcloud_sdk; then
    _ensure_prereqs_log_success "gcloud found (Google Cloud SDK)"
    return 0
  fi
  if _try_install_gcloud; then
    _ensure_prereqs_log_success "gcloud installed"
    return 0
  fi
  _ensure_prereqs_log_error "gcloud CLI is required for the Pub/Sub emulator"
  _ensure_prereqs_log_info "Install: https://cloud.google.com/sdk/docs/install"
  return 1
}

ensure_pubsub_emulator() {
  if ! ensure_gcloud; then
    return 1
  fi
  if ! ensure_java; then
    return 1
  fi
  if _pubsub_emulator_installed; then
    return 0
  fi
  if [[ "$ENSURE_PREREQS_AUTO_INSTALL" == "1" ]]; then
    _ensure_prereqs_log_info "Installing Pub/Sub emulator (gcloud component)..."
    if gcloud components install pubsub-emulator --quiet 2>/dev/null; then
      _ensure_prereqs_log_success "Pub/Sub emulator installed"
      return 0
    fi
  fi
  _ensure_prereqs_log_error "Pub/Sub emulator is not installed"
  _ensure_prereqs_log_info "Install: gcloud components install pubsub-emulator"
  return 1
}

_rclone_works() {
  command -v rclone &>/dev/null && rclone version &>/dev/null 2>&1
}

_try_install_rclone() {
  if [[ "$ENSURE_PREREQS_AUTO_INSTALL" != "1" ]]; then
    return 1
  fi
  case "$(uname -s)" in
    Darwin)
      if command -v brew &>/dev/null; then
        _ensure_prereqs_log_info "Installing rclone via Homebrew..."
        if brew install rclone &>/dev/null; then
          _rclone_works && return 0
        fi
      fi
      ;;
    Linux)
      if command -v apt-get &>/dev/null; then
        _ensure_prereqs_log_info "Installing rclone via apt..."
        if sudo apt-get install -y rclone &>/dev/null; then
          _rclone_works && return 0
        fi
      fi
      ;;
  esac
  return 1
}

ensure_rclone() {
  if _rclone_works; then
    return 0
  fi
  if _try_install_rclone; then
    _ensure_prereqs_log_success "rclone installed"
    return 0
  fi
  _ensure_prereqs_log_error "rclone is required for desktop file sync"
  _ensure_prereqs_log_info "macOS:  brew install rclone"
  _ensure_prereqs_log_info "Ubuntu: sudo apt install rclone"
  return 1
}

ensure_docker() {
  if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    return 0
  fi
  _ensure_prereqs_log_error "Docker is required for the self-host desktop container"
  _ensure_prereqs_log_info "Install Docker Desktop and ensure the daemon is running"
  return 1
}

_node_works() {
  if ! command -v node &>/dev/null || ! command -v npm &>/dev/null; then
    return 1
  fi
  local major
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  [[ "$major" -ge 20 ]]
}

_try_install_node() {
  if [[ "$ENSURE_PREREQS_AUTO_INSTALL" != "1" ]]; then
    return 1
  fi
  case "$(uname -s)" in
    Darwin)
      if command -v brew &>/dev/null; then
        _ensure_prereqs_log_info "Installing Node.js via Homebrew..."
        if brew install node &>/dev/null; then
          _node_works && return 0
        fi
      fi
      ;;
    Linux)
      if command -v apt-get &>/dev/null; then
        _ensure_prereqs_log_info "Installing Node.js via apt..."
        if sudo apt-get install -y nodejs npm &>/dev/null; then
          _node_works && return 0
        fi
      fi
      ;;
  esac
  return 1
}

ensure_node() {
  if _node_works; then
    return 0
  fi
  if _try_install_node; then
    _ensure_prereqs_log_success "Node.js installed ($(node --version 2>/dev/null))"
    return 0
  fi
  _ensure_prereqs_log_error "Node.js 20+ and npm are required for Console"
  _ensure_prereqs_log_info "macOS:  brew install node"
  _ensure_prereqs_log_info "Ubuntu: sudo apt install nodejs npm  (or use nvm/fnm for Node 20+)"
  return 1
}

ensure_self_host_stack_prereqs() {
  ensure_node && ensure_java && ensure_pubsub_emulator
}

ensure_self_host_prereqs() {
  ensure_self_host_stack_prereqs
}

ensure_self_host_desktop_prereqs() {
  ensure_docker && ensure_rclone
}
