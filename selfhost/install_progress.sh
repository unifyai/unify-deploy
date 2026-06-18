#!/usr/bin/env bash
# =============================================================================
# install_progress.sh — Unified [N/7] progress bars for install + setup
# =============================================================================
#
# Progress writes to /dev/tty when available so in-place \r updates work even
# when stdout/stderr are piped. Completed repo rows print above the live bar.
#
set -euo pipefail

INSTALL_PROGRESS_TOTAL="${INSTALL_PROGRESS_TOTAL:-7}"
INSTALL_PROGRESS_WIDTH="${INSTALL_PROGRESS_WIDTH:-10}"
_INSTALL_PROGRESS_STEP=0
_INSTALL_PROGRESS_LABEL=""
_INSTALL_PROGRESS_ACTIVE=false
_INSTALL_PROGRESS_PCT=0
_INSTALL_PROGRESS_SUFFIX=""
_INSTALL_PROGRESS_LAST_LINE=""
_INSTALL_PROGRESS_BAR_DRAWN=false
_INSTALL_PROGRESS_USE_TTY=false

_install_progress_init_tty() {
  _INSTALL_PROGRESS_USE_TTY=false
  if [[ -r /dev/tty && -w /dev/tty ]] && { : >/dev/tty; } 2>/dev/null; then
    _INSTALL_PROGRESS_USE_TTY=true
    return 0
  fi
  if [[ -t 2 ]]; then
    _INSTALL_PROGRESS_USE_TTY=true
  fi
}

_install_progress_target() {
  if [[ "$_INSTALL_PROGRESS_USE_TTY" == true && -w /dev/tty ]]; then
    echo /dev/tty
  elif [[ -t 2 ]]; then
    echo >&2
  else
    echo >&1
  fi
}

_install_progress_put() {
  local target="$(_install_progress_target)"
  if [[ "$target" == "/dev/tty" ]]; then
    printf '%s' "$1" >"$target"
  else
    printf '%s' "$1" >&2
  fi
}

_install_progress_putln() {
  local target="$(_install_progress_target)"
  if [[ "$target" == "/dev/tty" ]]; then
    printf '%s\n' "$1" >"$target"
  elif [[ -t 2 ]]; then
    printf '%s\n' "$1" >&2
  else
    printf '%s\n' "$1"
  fi
}

_install_progress_bar() {
  local pct="${1:-0}"
  if (( pct < 0 )); then pct=0; fi
  if (( pct > 100 )); then pct=100; fi
  local filled=$(( pct * INSTALL_PROGRESS_WIDTH / 100 ))
  local empty=$(( INSTALL_PROGRESS_WIDTH - filled ))
  local bar=""
  local i
  for ((i = 0; i < filled; i++)); do bar+='█'; done
  for ((i = 0; i < empty; i++)); do bar+='░'; done
  printf '%s' "$bar"
}

_install_progress_format_line() {
  local step="$1"
  local label="$2"
  local pct="$3"
  local suffix="${4:-}"
  if [[ -n "$suffix" ]]; then
    printf '[%d/%d] %s… %s %3d%% %s' \
      "$step" "$INSTALL_PROGRESS_TOTAL" "$label" "$(_install_progress_bar "$pct")" "$pct" "$suffix"
  else
    printf '[%d/%d] %s… %s %3d%%' \
      "$step" "$INSTALL_PROGRESS_TOTAL" "$label" "$(_install_progress_bar "$pct")" "$pct"
  fi
}

_install_progress_current_line() {
  _install_progress_format_line "$_INSTALL_PROGRESS_STEP" "$_INSTALL_PROGRESS_LABEL" "$_INSTALL_PROGRESS_PCT" "$_INSTALL_PROGRESS_SUFFIX"
}

_install_progress_can_animate() {
  [[ "$_INSTALL_PROGRESS_USE_TTY" == true || -t 2 ]]
}

_install_progress_render_bar() {
  local redraw="${1:-false}"
  local line="$(_install_progress_current_line)"

  if _install_progress_can_animate; then
    if [[ "$redraw" != true && "$_INSTALL_PROGRESS_BAR_DRAWN" == true && "$line" == "$_INSTALL_PROGRESS_LAST_LINE" ]]; then
      return 0
    fi
    if [[ "$redraw" == true || "$_INSTALL_PROGRESS_BAR_DRAWN" != true ]]; then
      _install_progress_put "$line"
      _INSTALL_PROGRESS_BAR_DRAWN=true
    else
      _install_progress_put $'\r\033[2K'
      _install_progress_put "$line"
    fi
    _INSTALL_PROGRESS_LAST_LINE="$line"
    return 0
  fi

  if [[ "$line" != "$_INSTALL_PROGRESS_LAST_LINE" ]]; then
    _install_progress_putln "$line"
    _INSTALL_PROGRESS_LAST_LINE="$line"
  fi
}

progress_step_begin() {
  _install_progress_init_tty
  _INSTALL_PROGRESS_STEP="$1"
  _INSTALL_PROGRESS_LABEL="$2"
  _INSTALL_PROGRESS_ACTIVE=true
  _INSTALL_PROGRESS_PCT=0
  _INSTALL_PROGRESS_SUFFIX=""
  _INSTALL_PROGRESS_LAST_LINE=""
  _INSTALL_PROGRESS_BAR_DRAWN=false
  _install_progress_render_bar false
}

progress_step_update() {
  local pct="$1"
  local suffix="${2:-}"
  if [[ "$_INSTALL_PROGRESS_ACTIVE" != true ]]; then
    return 0
  fi
  _INSTALL_PROGRESS_PCT="$pct"
  _INSTALL_PROGRESS_SUFFIX="$suffix"
  _install_progress_render_bar false
}

progress_step_end_success() {
  if [[ "$_INSTALL_PROGRESS_ACTIVE" != true ]]; then
    return 0
  fi
  _INSTALL_PROGRESS_PCT=100
  _INSTALL_PROGRESS_SUFFIX=""
  if _install_progress_can_animate; then
    _install_progress_put $'\r\033[2K'
    _install_progress_putln "$(_install_progress_current_line)"
  else
    _install_progress_putln "$(_install_progress_current_line)"
  fi
  _INSTALL_PROGRESS_ACTIVE=false
  _INSTALL_PROGRESS_BAR_DRAWN=false
  _INSTALL_PROGRESS_LAST_LINE=""
}

progress_step_end_fail() {
  if [[ "$_INSTALL_PROGRESS_ACTIVE" != true ]]; then
    return 1
  fi
  _INSTALL_PROGRESS_PCT=0
  _INSTALL_PROGRESS_SUFFIX="(failed)"
  if _install_progress_can_animate; then
    _install_progress_put $'\r\033[2K'
    _install_progress_putln "$(_install_progress_current_line)"
  else
    _install_progress_putln "$(_install_progress_current_line)"
  fi
  _INSTALL_PROGRESS_ACTIVE=false
  _INSTALL_PROGRESS_BAR_DRAWN=false
  _INSTALL_PROGRESS_LAST_LINE=""
  return 1
}

progress_step_run() {
  local step="$1"
  local label="$2"
  shift 2
  progress_step_begin "$step" "$label"
  local pct=8
  local last_pct=-1
  "$@" &
  local cmd_pid=$!
  while kill -0 "$cmd_pid" 2>/dev/null; do
    if (( pct != last_pct )); then
      progress_step_update "$pct"
      last_pct=$pct
    fi
    pct=$(( pct + 9 ))
    if (( pct > 95 )); then pct=12; fi
    sleep 0.35
  done
  if wait "$cmd_pid"; then
    progress_step_end_success
    return 0
  fi
  progress_step_end_fail
  return 1
}

progress_repo_line() {
  local name="$1"
  local detail="$2"
  if _install_progress_can_animate; then
    _install_progress_putln "$(printf '       ✓ %-10s %s' "$name" "$detail")"
    _install_progress_render_bar true
  else
    _install_progress_putln "$(printf '       ✓ %-10s %s' "$name" "$detail")"
  fi
}

progress_repo_fail() {
  local name="$1"
  local detail="$2"
  if _install_progress_can_animate; then
    _install_progress_putln "$(printf '       ✗ %-10s %s' "$name" "$detail")"
    _install_progress_render_bar true
  else
    _install_progress_putln "$(printf '       ✗ %-10s %s' "$name" "$detail")"
  fi
}
