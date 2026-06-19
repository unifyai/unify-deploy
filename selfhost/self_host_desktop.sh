#!/usr/bin/env bash
# =============================================================================
# self_host_desktop.sh — Self-host managed desktop (Docker + Caddy proxy + SFTP)
# =============================================================================
#
# Brings up production-shaped desktop_url for the Coordinator:
#   desktop_url = http://127.0.0.1:8090
#     /desktop/custom.html  -> noVNC (host :6080)
#     /api/*                -> agent-service (host :13000, container :3000)
#   SFTP unityuser@127.0.0.1:2222 -> /Droid/Local in container volume
#
# Usage:
#   ensure_self_host_desktop <agent_id> <unify_key>
#   publish_self_host_desktop_ready <agent_id>
#   stop_self_host_desktop
#
set -euo pipefail

# This script lives in droid-deploy/selfhost/; the desktop image and droid venv
# come from the sibling droid checkout under UNIFY_STACK_ROOT.
SELF_HOST_DESKTOP_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_DEPLOY_REPO_PATH="$(cd "$SELF_HOST_DESKTOP_SCRIPT_DIR/.." && pwd)"
UNIFY_STACK_ROOT="${UNIFY_STACK_ROOT:-$(cd "$_DEPLOY_REPO_PATH/.." && pwd)}"
DROID_REPO="${DROID_REPO:-${DROID_REPO_PATH:-$UNIFY_STACK_ROOT/droid}}"

SELF_HOST_DESKTOP_IMAGE="${SELF_HOST_DESKTOP_IMAGE:-droid-desktop}"
SELF_HOST_DESKTOP_CONTAINER="${SELF_HOST_DESKTOP_CONTAINER:-droid-desktop-selfhost}"
SELF_HOST_DESKTOP_PROXY_CONTAINER="${SELF_HOST_DESKTOP_PROXY_CONTAINER:-droid-desktop-proxy}"
SELF_HOST_DESKTOP_VOLUME="${SELF_HOST_DESKTOP_VOLUME:-droid-desktop-local}"
SELF_HOST_DESKTOP_URL="${SELF_HOST_DESKTOP_URL:-http://127.0.0.1:8090}"
SELF_HOST_DESKTOP_PROXY_PORT="${SELF_HOST_DESKTOP_PROXY_PORT:-8090}"
# Host port for agent-service (container listens on 3000). Avoid 3000 — Console uses it.
SELF_HOST_DESKTOP_AGENT_PORT="${SELF_HOST_DESKTOP_AGENT_PORT:-13000}"
SELF_HOST_DESKTOP_NOVNC_PORT="${SELF_HOST_DESKTOP_NOVNC_PORT:-6080}"
SELF_HOST_DESKTOP_SFTP_PORT="${SELF_HOST_DESKTOP_SFTP_PORT:-2222}"

_self_host_desktop_log() {
  echo "→ [self-host desktop] $*"
}

_self_host_desktop_log_ok() {
  echo "✓ [self-host desktop] $*"
}

_self_host_desktop_log_warn() {
  echo "⚠ [self-host desktop] $*" >&2
}

_docker_ready() {
  command -v docker &>/dev/null && docker info &>/dev/null 2>&1
}

_desktop_image_exists() {
  docker image inspect "$SELF_HOST_DESKTOP_IMAGE" &>/dev/null 2>&1
}

_build_desktop_image() {
  local dockerfile="$DROID_REPO/deploy/desktop/Dockerfile"
  if [[ ! -f "$dockerfile" ]]; then
    _self_host_desktop_log_warn "Missing Dockerfile at $dockerfile"
    return 1
  fi
  _self_host_desktop_log "Building Docker image '$SELF_HOST_DESKTOP_IMAGE' (first run may take several minutes)..."
  docker build -t "$SELF_HOST_DESKTOP_IMAGE" -f "$dockerfile" "$DROID_REPO"
  _self_host_desktop_log_ok "Image '$SELF_HOST_DESKTOP_IMAGE' built"
}

_agent_service_healthy() {
  local unify_key="$1"
  local url="http://127.0.0.1:${SELF_HOST_DESKTOP_PROXY_PORT}/api/sessions"
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${unify_key}" \
    --connect-timeout 2 --max-time 5 \
    "$url" 2>/dev/null || echo "000")"
  [[ "$code" == "200" ]]
}

_sftp_healthy() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' \
    --connect-timeout 1 --max-time 2 \
    "http://127.0.0.1:${SELF_HOST_DESKTOP_PROXY_PORT}/" 2>/dev/null || echo "000")"
  # Proxy may return 404 on / — that's fine; we only care that something listens.
  [[ "$code" != "000" ]]
}

_ensure_desktop_ssh_public_key() {
  local agent_id="$1"
  local orchestra_url="${ORCHESTRA_URL:-http://127.0.0.1:8000/v0}"
  local admin_key="${ORCHESTRA_ADMIN_KEY:-}"
  if [[ -z "$admin_key" ]]; then
    _self_host_desktop_log_warn "ORCHESTRA_ADMIN_KEY is not set — cannot provision SFTP key"
    return 1
  fi
  local py="${DROID_REPO}/.venv/bin/python"
  if [[ ! -x "$py" ]]; then
    py="python3"
  fi
  "$py" "$_DEPLOY_REPO_PATH/deploy/selfhost/ensure_self_host_desktop_ssh_key.py" \
    --agent-id "$agent_id" \
    --orchestra-url "$orchestra_url" \
    --admin-key "$admin_key" \
    --output public
}

_sync_novnc_custom_html() {
  local custom_html="$DROID_REPO/deploy/desktop/novnc/custom.html"
  if [[ ! -f "$custom_html" ]]; then
    return 0
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx "$SELF_HOST_DESKTOP_CONTAINER"; then
    return 0
  fi
  docker cp "$custom_html" "${SELF_HOST_DESKTOP_CONTAINER}:/opt/novnc/custom.html" >/dev/null
}

_disable_desktop_screen_blanking() {
  # xfce4-screensaver re-arms the X server screensaver timeout (300s) at
  # session startup even with the saver disabled in xfconf. After idle, the
  # framebuffer blanks and the view-only liveview iframe shows a black screen
  # that only real input would wake. Stop the saver and force the timeout off
  # so images built before the Dockerfile removed the autostart entries still
  # render.
  if ! docker ps --format '{{.Names}}' | grep -qx "$SELF_HOST_DESKTOP_CONTAINER"; then
    return 0
  fi
  docker exec "$SELF_HOST_DESKTOP_CONTAINER" sh -c '
    rm -f /etc/xdg/autostart/xfce4-screensaver.desktop /etc/xdg/autostart/xscreensaver.desktop
    pkill -x xfce4-screensaver 2>/dev/null
    DISPLAY=:99 xset s off s noblank 2>/dev/null
    true
  ' >/dev/null 2>&1 || true
}

_start_desktop_container() {
  local agent_id="$1"
  local unify_key="$2"
  local ssh_public_key="$3"

  if docker ps --format '{{.Names}}' | grep -qx "$SELF_HOST_DESKTOP_CONTAINER"; then
    if _agent_service_healthy "$unify_key"; then
      _self_host_desktop_log_ok "Desktop container '$SELF_HOST_DESKTOP_CONTAINER' already running"
      return 0
    fi
    _self_host_desktop_log "Restarting unhealthy desktop container..."
    docker rm -f "$SELF_HOST_DESKTOP_CONTAINER" >/dev/null 2>&1 || true
  elif docker ps -a --format '{{.Names}}' | grep -qx "$SELF_HOST_DESKTOP_CONTAINER"; then
    docker rm -f "$SELF_HOST_DESKTOP_CONTAINER" >/dev/null 2>&1 || true
  fi

  if ! _desktop_image_exists; then
    _build_desktop_image || return 1
  fi

  docker volume create "$SELF_HOST_DESKTOP_VOLUME" >/dev/null

  local env_file="$DROID_REPO/.env"
  local orchestra_url="${ORCHESTRA_URL:-http://127.0.0.1:8000/v0}"
  if [[ "$orchestra_url" == *127.0.0.1* || "$orchestra_url" == *localhost* ]]; then
    orchestra_url="${orchestra_url//127.0.0.1/host.docker.internal}"
    orchestra_url="${orchestra_url//localhost/host.docker.internal}"
  fi

  local -a run_cmd=(
    docker run -d --name "$SELF_HOST_DESKTOP_CONTAINER"
    -p "${SELF_HOST_DESKTOP_NOVNC_PORT}:6080"
    -p "${SELF_HOST_DESKTOP_AGENT_PORT}:3000"
    -p "${SELF_HOST_DESKTOP_SFTP_PORT}:2222"
    -v "${SELF_HOST_DESKTOP_VOLUME}:/Droid/Local"
    -e "DROID_SSH_PUBLIC_KEY=${ssh_public_key}"
    -e "UNIFY_KEY=${unify_key}"
    -e "DROID_LOCAL_ROOT=/Droid/Local"
    -e "ORCHESTRA_URL=${orchestra_url}"
    --add-host=host.docker.internal:host-gateway
  )
  if [[ -f "$env_file" ]]; then
    run_cmd+=(--env-file "$env_file")
    run_cmd+=(-e "ORCHESTRA_URL=${orchestra_url}")
  fi
  run_cmd+=("$SELF_HOST_DESKTOP_IMAGE")

  "${run_cmd[@]}" >/dev/null
  _self_host_desktop_log "Started desktop container '$SELF_HOST_DESKTOP_CONTAINER'"
}

_write_desktop_proxy_caddyfile() {
  local caddyfile="$1"
  cat >"$caddyfile" <<EOF
# Generated by self_host_desktop.sh — do not edit by hand.
:8090 {
	log {
		output stdout
		format console
	}
	# Heal browsers that cached the old custom.html, which redirected to the
	# root /vnc.html path instead of the proxied /desktop/vnc.html.
	redir /vnc.html /desktop{uri}
	handle_path /desktop/* {
		# custom.html is a redirect shim that gets hot-patched; never let
		# browsers cache it or they replay stale bootstrap logic.
		header /custom.html Cache-Control no-store
		reverse_proxy host.docker.internal:${SELF_HOST_DESKTOP_NOVNC_PORT}
	}
	handle_path /api/* {
		reverse_proxy host.docker.internal:${SELF_HOST_DESKTOP_AGENT_PORT}
	}
}
EOF
}

_start_desktop_proxy() {
  local caddy_dir="${DROID_REPO}/.droid/self-host-desktop-caddy"
  mkdir -p "$caddy_dir"
  _write_desktop_proxy_caddyfile "${caddy_dir}/Caddyfile"

  if docker ps --format '{{.Names}}' | grep -qx "$SELF_HOST_DESKTOP_PROXY_CONTAINER"; then
    if _sftp_healthy; then
      _self_host_desktop_log_ok "Desktop proxy '$SELF_HOST_DESKTOP_PROXY_CONTAINER' already running"
      return 0
    fi
    docker rm -f "$SELF_HOST_DESKTOP_PROXY_CONTAINER" >/dev/null 2>&1 || true
  elif docker ps -a --format '{{.Names}}' | grep -qx "$SELF_HOST_DESKTOP_PROXY_CONTAINER"; then
    docker rm -f "$SELF_HOST_DESKTOP_PROXY_CONTAINER" >/dev/null 2>&1 || true
  fi

  if ! docker run -d --name "$SELF_HOST_DESKTOP_PROXY_CONTAINER" \
    -p "${SELF_HOST_DESKTOP_PROXY_PORT}:8090" \
    -v "${caddy_dir}/Caddyfile:/etc/caddy/Caddyfile:ro" \
    --add-host=host.docker.internal:host-gateway \
    caddy:2-alpine >/dev/null; then
    _self_host_desktop_log_warn "Failed to start desktop proxy container"
    return 1
  fi

  _self_host_desktop_log_ok "Started desktop proxy on ${SELF_HOST_DESKTOP_URL}"
}

_wait_for_desktop_ready() {
  local unify_key="$1"
  local deadline=$((SECONDS + 180))
  local last_log="$SECONDS"
  while (( SECONDS < deadline )); do
    if _agent_service_healthy "$unify_key" && _sftp_healthy; then
      _self_host_desktop_log_ok "Desktop services are healthy"
      return 0
    fi
    if (( SECONDS - last_log >= 15 )); then
      _self_host_desktop_log "Waiting for agent-service on :${SELF_HOST_DESKTOP_AGENT_PORT} (via ${SELF_HOST_DESKTOP_URL}/api)..."
      last_log=$SECONDS
    fi
    sleep 2
  done
  _self_host_desktop_log_warn "Timed out waiting for desktop services (check agent on host :${SELF_HOST_DESKTOP_AGENT_PORT}, Console must not use that port)"
  return 1
}

ensure_self_host_desktop() {
  local agent_id="${1:-}"
  local unify_key="${2:-}"
  if [[ -z "$agent_id" || -z "$unify_key" ]]; then
    _self_host_desktop_log_warn "agent_id and unify_key are required"
    return 1
  fi
  if ! _docker_ready; then
    _self_host_desktop_log_warn "Docker is not available"
    return 1
  fi
  if ! _desktop_image_exists; then
    _build_desktop_image || return 1
  fi

  local ssh_public_key
  ssh_public_key="$(_ensure_desktop_ssh_public_key "$agent_id")" || return 1

  _start_desktop_container "$agent_id" "$unify_key" "$ssh_public_key" || return 1
  _sync_novnc_custom_html
  _start_desktop_proxy || return 1
  _wait_for_desktop_ready "$unify_key" || return 1
  _disable_desktop_screen_blanking

  export SELF_HOST_DESKTOP_URL
  _self_host_desktop_log_ok "Desktop ready at ${SELF_HOST_DESKTOP_URL}"
}

publish_self_host_desktop_ready() {
  local agent_id="${1:-}"
  if [[ -z "$agent_id" ]]; then
    _self_host_desktop_log_warn "agent_id is required"
    return 1
  fi
  local py="${DROID_REPO}/.venv/bin/python"
  if [[ ! -x "$py" ]]; then
    py="python3"
  fi
  "$py" "$_DEPLOY_REPO_PATH/deploy/selfhost/publish_self_host_desktop_ready.py" \
    --assistant-id "$agent_id" \
    --desktop-url "$SELF_HOST_DESKTOP_URL"
}

stop_self_host_desktop() {
  docker rm -f "$SELF_HOST_DESKTOP_CONTAINER" "$SELF_HOST_DESKTOP_PROXY_CONTAINER" >/dev/null 2>&1 || true
  _self_host_desktop_log_ok "Stopped self-host desktop containers"
}

case "${1:-}" in
  ensure)
    ensure_self_host_desktop "${2:-}" "${3:-}"
    ;;
  publish-ready)
    publish_self_host_desktop_ready "${2:-}"
    ;;
  stop)
    stop_self_host_desktop
    ;;
  *)
    echo "Usage: $0 {ensure|publish-ready|stop} ..." >&2
    exit 1
    ;;
esac
