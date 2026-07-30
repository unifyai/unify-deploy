#!/bin/bash

# Exit on any error
set -e

# Record container start time (milliseconds since epoch).
# Used by the Python process to compute container spin-up duration (U1).
export CONTAINER_START_TIME_MS=$(date +%s%3N)

# Global variables to track processes
MAIN_PID=""
OFFLINE_PID=""
AGENT_PID=""
WATCHDOG_PID=""
DISPLAY_PID=""
DEVICE_PID=""

uptime_ms() {
    now_ms=$(date +%s%3N)
    echo $((now_ms - CONTAINER_START_TIME_MS))
}

shutdown_reason() {
    if [ -f /tmp/oom_prevention_shutdown ]; then
        echo "oom_prevention"
    else
        echo "external_sigterm"
    fi
}

memory_watchdog() {
    local watch_pid="$1"
    if [ -f /sys/fs/cgroup/memory.max ]; then
        max_file=/sys/fs/cgroup/memory.max
        current_file=/sys/fs/cgroup/memory.current
    elif [ -f /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
        max_file=/sys/fs/cgroup/memory/memory.limit_in_bytes
        current_file=/sys/fs/cgroup/memory/memory.usage_in_bytes
    else
        echo "[MEMORY_WATCHDOG] No cgroup memory files found, watchdog disabled"
        return
    fi

    max_bytes=$(cat "$max_file")
    if [ "$max_bytes" = "max" ]; then
        echo "[MEMORY_WATCHDOG] No memory limit set, watchdog disabled"
        return
    fi

    threshold_pct=${MEMORY_WATCHDOG_THRESHOLD:-90}
    interval=${MEMORY_WATCHDOG_INTERVAL:-5}
    threshold_bytes=$((max_bytes * threshold_pct / 100))

    echo "[MEMORY_WATCHDOG] Limit: $((max_bytes / 1048576))MiB, threshold: ${threshold_pct}% ($((threshold_bytes / 1048576))MiB), check every ${interval}s"

    while kill -0 "$watch_pid" 2>/dev/null; do
        sleep "$interval"
        current_bytes=$(cat "$current_file" 2>/dev/null) || continue
        if [ "$current_bytes" -ge "$threshold_bytes" ]; then
            pct=$((current_bytes * 100 / max_bytes))
            echo "$(date '+%Y-%m-%d %H:%M:%S.%3N') - [MEMORY_WATCHDOG] Usage at ${pct}% ($((current_bytes / 1048576))/$((max_bytes / 1048576))MiB) — triggering graceful shutdown"
            touch /tmp/oom_prevention_shutdown
            kill -TERM "$watch_pid" 2>/dev/null || true
            return
        fi
    done
}

AGENT_SERVICE_PORT="${PORT:-3000}"

# The agent-service is part of the shared desktop substrate, not an
# interactive-only nicety: web sessions reach it over localhost regardless of
# whether the pod is running the ConversationManager or a one-shot offline task
# runner. It used to be started only by the CM's restart-on-key-change path,
# which left offline pods with nothing listening — a browser-driven scheduled
# task then failed to connect and looked like a scraper bug.
#
# Comes up with the container's own UNIFY_KEY. Interactive pods respawn it with
# the user's key once session details land (see ``_restart_agent_service_with_key``);
# offline pods already carry the assistant's own key, so this start is final.
start_agent_service() {
    local agent_dir="/app/agent-service"
    if [ ! -d "$agent_dir" ]; then
        echo "⬥ agent-service directory not found at $agent_dir, skipping"
        return
    fi

    echo "⬥ Starting agent-service..."
    if [ -f "$agent_dir/dist/index.js" ]; then
        set -- node --no-deprecation "$agent_dir/dist/index.js"
    else
        set -- npx ts-node "$agent_dir/src/index.ts"
    fi

    (
        cd "$agent_dir" || exit 0
        PLAYWRIGHT_BROWSERS_PATH="/home/unity/.cache/ms-playwright" \
            "$@" >>/var/log/unity/agent-service.log 2>&1 &
        echo $! >/tmp/agent-service.pid
    )
    AGENT_PID=$(cat /tmp/agent-service.pid 2>/dev/null || true)
}

# Readiness is a TCP listen check rather than an HTTP probe: the service
# exposes no unauthenticated health route, and every real caller only needs the
# socket to be accepting. Bounded and non-fatal — a wedged start should surface
# as a connect error from the caller, not a pod that never boots.
wait_for_agent_service() {
    [ -f /tmp/agent-service.pid ] || return 0
    local deadline=$((SECONDS + 30))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(1)
sys.exit(0 if s.connect_ex(('127.0.0.1', $AGENT_SERVICE_PORT)) == 0 else 1)
" 2>/dev/null; then
            echo "⬥ agent-service listening on $AGENT_SERVICE_PORT (PID: ${AGENT_PID:-unknown})"
            return 0
        fi
        sleep 1
    done
    echo "⬥ agent-service not listening on $AGENT_SERVICE_PORT after 30s; browser sessions will fail"
}

stop_agent_service() {
    local pid_file="/tmp/agent-service.pid"
    local pid=""
    if [ -f "$pid_file" ]; then
        pid=$(cat "$pid_file")
    elif [ ! -z "$AGENT_PID" ]; then
        pid=$AGENT_PID
    fi

    if [ ! -z "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "Stopping agent-service (PID: $pid)..."
        kill -TERM "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    else
        echo "Stopping agent-service..."
        pkill -f "ts-node" 2>/dev/null || true
    fi
}

on_signal() {
    local reason
    reason=$(shutdown_reason)
    echo "$(date '+%Y-%m-%d %H:%M:%S.%3N') - [ENTRYPOINT] Received shutdown signal (reason=${reason}, uptime_ms=$(uptime_ms), main_pid=${MAIN_PID:-none}, offline_pid=${OFFLINE_PID:-none}), cleaning up..."

    if [ ! -z "$WATCHDOG_PID" ]; then
        kill $WATCHDOG_PID 2>/dev/null || true
    fi
    if [ ! -z "$MAIN_PID" ]; then
        echo "Stopping main application (PID: $MAIN_PID)..."
        kill -TERM $MAIN_PID 2>/dev/null || true
        wait $MAIN_PID 2>/dev/null || true
    fi
    # Disconnected offline runners are allowed to finish; do not SIGKILL them.
    stop_agent_service
    if [ ! -z "$DEVICE_PID" ]; then
        kill $DEVICE_PID 2>/dev/null || true
    fi
    if [ ! -z "$DISPLAY_PID" ]; then
        kill $DISPLAY_PID 2>/dev/null || true
    fi
    echo "Cleanup complete"
    exit 0
}

trap on_signal SIGTERM SIGINT

mkdir -p /var/log/unity /var/log/unisdk /var/log/unillm &

if [ ! -z "$UNITY_CONVERSATION_JOB_NAME" ]; then
    _GCS_BUCKET="${GCS_LOG_BUCKET:-unity-pod-logs}"
    case "$UNITY_CONVERSATION_JOB_NAME" in
        *-staging)    _NS="staging" ;;
        *-production) _NS="production" ;;
        *)            _NS="unknown" ;;
    esac
    _GCS_PATH="gs://${_GCS_BUCKET}/${_NS}/${UNITY_CONVERSATION_JOB_NAME}/"
    echo "═══════════════════════════════════════════════════════════"
    echo "  Pod logs will be uploaded on shutdown to:"
    echo "  ${_GCS_PATH}"
    echo "  (auto-deleted after 7 days)"
    echo "═══════════════════════════════════════════════════════════"
fi

if [ -d /opt/hf-cache ] && [ ! -d /tmp/huggingface ]; then
    cp -r /opt/hf-cache /tmp/huggingface &
fi

# ── Desktop stack (always — same substrate for offline and interactive) ──
export XDG_RUNTIME_DIR=/tmp/runtime-unity
mkdir -p "$XDG_RUNTIME_DIR" 2>/dev/null || true
chmod 700 "$XDG_RUNTIME_DIR" 2>/dev/null || true

dbus-daemon --system --fork 2>/dev/null || true
eval "$(dbus-launch)"
export DBUS_SESSION_BUS_ADDRESS

echo "⬥ Starting virtual display..."
bash /app/deploy/desktop/display.sh &
DISPLAY_PID=$!

echo "⬥ Starting virtual audio devices..."
bash /app/deploy/desktop/device.sh &
DEVICE_PID=$!

# Spawned before the display settles so node's startup overlaps the wait below.
start_agent_service

sleep 3

wait_for_agent_service

start_conversation_manager() {
    echo "⬥ Starting convo manager..."
    python3 unify/conversation_manager/main.py &
    MAIN_PID=$!
    echo "⬥ Main application started with PID: $MAIN_PID"
    memory_watchdog "$MAIN_PID" &
    WATCHDOG_PID=$!
}

# One-shot offline task-run pods carry the full runner contract in the
# container env (UNITY_OFFLINE_TASK_*); everything else is interactive.
if [ -n "${UNITY_OFFLINE_TASK_MODE:-}" ]; then
    echo "⬥ Offline task-run pod (mode=${UNITY_OFFLINE_TASK_MODE})"
    echo "⬥ Fetching client bundle for offline task runner..."
    python3 -m unify_deploy.client_bundle.bootstrap || python3 -c "from unify_deploy.client_bundle.bootstrap import ensure_offline_client_bundle; ensure_offline_client_bundle()" || true
    echo "⬥ Starting offline task runner..."
    python3 -m unify.task_scheduler.offline_runner &
    OFFLINE_PID=$!
    memory_watchdog "$OFFLINE_PID" &
    WATCHDOG_PID=$!
    OFFLINE_EXIT_CODE=0
    wait $OFFLINE_PID || OFFLINE_EXIT_CODE=$?
    echo "$(date '+%Y-%m-%d %H:%M:%S.%3N') - [ENTRYPOINT] Offline runner exited (code=${OFFLINE_EXIT_CODE}, uptime_ms=$(uptime_ms))"
    if [ "$OFFLINE_EXIT_CODE" -ne 0 ]; then
        # Propagate failure so the Job reports Failed instead of Complete.
        if [ ! -z "$WATCHDOG_PID" ]; then
            kill $WATCHDOG_PID 2>/dev/null || true
        fi
        stop_agent_service
        kill $DEVICE_PID 2>/dev/null || true
        kill $DISPLAY_PID 2>/dev/null || true
        exit "$OFFLINE_EXIT_CODE"
    fi
else
    start_conversation_manager
    MAIN_EXIT_CODE=0
    wait $MAIN_PID || MAIN_EXIT_CODE=$?
    echo "$(date '+%Y-%m-%d %H:%M:%S.%3N') - [ENTRYPOINT] Main process exited (code=${MAIN_EXIT_CODE}, uptime_ms=$(uptime_ms))"
fi

if [ ! -z "$WATCHDOG_PID" ]; then
    kill $WATCHDOG_PID 2>/dev/null || true
fi
stop_agent_service
kill $DEVICE_PID 2>/dev/null || true
kill $DISPLAY_PID 2>/dev/null || true
