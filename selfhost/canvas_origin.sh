#!/usr/bin/env bash
#
# Lifecycle for the local Canvas origin.
#
# Canvas frames assistant-authored code from an origin separate from Console, so
# that the frame's CSP is one we own and Console's is never relaxed to accommodate
# it. Production gets that from `canvas.unify.ai`; self-host gets it from a port,
# which is part of an origin and therefore just as real to the browser.
#
# Sourced by stack.sh. Every function is a no-op when the runtime host has not been
# installed, because `setup.sh` deliberately does not gate on the canvas build —
# the rest of the stack works without it, and only canvas is unavailable.

CANVAS_HOST_DIR="${CANVAS_HOST_DIR:-$HOME/.unity/canvas-host}"
CANVAS_PORT="${CANVAS_PORT:-3100}"
CANVAS_ORIGIN="${CANVAS_ORIGIN:-http://localhost:${CANVAS_PORT}}"
CANVAS_ORIGIN_PID_FILE="${CANVAS_ORIGIN_PID_FILE:-${UNITY_HOME:-$HOME/.unity}/canvas-origin.pid}"
CANVAS_ORIGIN_LOG_FILE="${CANVAS_ORIGIN_LOG_FILE:-${UNITY_HOME:-$HOME/.unity}/canvas-origin.log}"

canvas_origin_installed() {
    [[ -f "$CANVAS_HOST_DIR/host/v1/index.html" ]]
}

canvas_origin_pid() {
    [[ -f "$CANVAS_ORIGIN_PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$CANVAS_ORIGIN_PID_FILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    printf '%s' "$pid"
}

canvas_origin_is_running() {
    canvas_origin_pid >/dev/null 2>&1
}

# Exported for Console (`frame-src` and the client origin) and for the host's own
# `frame-ancestors`. All three must name the same pair or the frame will not load,
# which is why they are derived here rather than set in three env files.
canvas_origin_export_env() {
    export CANVAS_ORIGIN
    export NEXT_PUBLIC_CANVAS_ORIGIN="$CANVAS_ORIGIN"
    export CANVAS_ALLOWED_CONSOLE_ORIGINS="${CANVAS_ALLOWED_CONSOLE_ORIGINS:-http://localhost:${CONSOLE_PORT:-3000}}"
}

canvas_origin_start() {
    if ! canvas_origin_installed; then
        log_warn "Canvas runtime host not installed — canvas views will not render."
        log_info "  Install with: bash $(dirname "${BASH_SOURCE[0]}")/setup.sh"
        return 0
    fi

    canvas_origin_export_env

    if canvas_origin_is_running; then
        log_success "Canvas origin already serving ${CANVAS_ORIGIN}"
        return 0
    fi

    if ! command -v node >/dev/null 2>&1; then
        log_warn "node not found — canvas origin not started."
        return 0
    fi

    mkdir -p "$(dirname "$CANVAS_ORIGIN_PID_FILE")"

    # setsid where available so the server outlives the shell that started it;
    # a one-shot agent or script shell exiting must not take the origin with it.
    if command -v setsid >/dev/null 2>&1; then
        setsid node "$(dirname "${BASH_SOURCE[0]}")/canvas_origin.mjs" \
            >>"$CANVAS_ORIGIN_LOG_FILE" 2>&1 &
    else
        node "$(dirname "${BASH_SOURCE[0]}")/canvas_origin.mjs" \
            >>"$CANVAS_ORIGIN_LOG_FILE" 2>&1 &
        disown
    fi
    echo $! >"$CANVAS_ORIGIN_PID_FILE"

    local attempt
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        if curl -sf -o /dev/null "${CANVAS_ORIGIN}/host/v1/index.html"; then
            log_success "Canvas origin serving ${CANVAS_ORIGIN}"
            return 0
        fi
        sleep 0.3
    done

    log_warn "Canvas origin did not answer on ${CANVAS_ORIGIN} — see $CANVAS_ORIGIN_LOG_FILE"
    return 0
}

canvas_origin_stop() {
    local pid
    if pid="$(canvas_origin_pid)"; then
        kill "$pid" 2>/dev/null || true
    fi
    rm -f "$CANVAS_ORIGIN_PID_FILE"
}

canvas_origin_status_line() {
    if ! canvas_origin_installed; then
        printf 'Canvas origin: not installed\n'
        return 0
    fi
    if canvas_origin_is_running; then
        printf 'Canvas origin: running (%s)\n' "$CANVAS_ORIGIN"
    else
        printf 'Canvas origin: stopped\n'
    fi
}
