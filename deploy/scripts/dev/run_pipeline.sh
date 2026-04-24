#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# run_pipeline.sh — Dispatch a pipeline job and stream worker logs to files
#
# Modes:
#   dispatch (default)  Dispatch + stream logs + monitor scaling
#   monitor             Attach to an already-running pipeline (logs + scaling only)
#
# Usage:
#   # Dispatch a new job and start monitoring
#   deploy/scripts/dev/run_pipeline.sh \
#     --mode dm \
#     --config path/to/pipeline_config.json \
#     --project-root ~/unity-deploy \
#     --user-id UUID --assistant-id 1823 \
#     [--limit 5] [extra dispatch_pipeline.py flags...]
#
#   # Monitor only (no dispatch — attach to workers already processing)
#   deploy/scripts/dev/run_pipeline.sh --monitor
#
# Creates a timestamped log directory under logs/pipeline/ with:
#   dispatch.log        — dispatch_pipeline.py output (dispatch mode only)
#   parse-worker.log    — streaming kubectl logs for parse pods
#   ingest-worker.log   — streaming kubectl logs for ingest pods
#   hpa.log             — periodic HPA snapshots (every 10s)
#   pods.log            — periodic pod-count snapshots (every 10s)
#   pubsub-backlog.log  — periodic Pub/Sub backlog depth (every 15s)
#   summary.txt         — final run summary
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"

# ---- Parse our own flags before forwarding the rest ----
MONITOR_ONLY=0
DISPATCH_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --monitor) MONITOR_ONLY=1 ;;
    *)         DISPATCH_ARGS+=("$arg") ;;
  esac
done

WORKER_NS="${UNITY_GCP_PIPELINE_ENVIRONMENT:-staging}"

RUN_TS="$(date +%Y-%m-%dT%H-%M-%S)"
LOG_DIR="$REPO_ROOT/logs/pipeline/${RUN_TS}"
mkdir -p "$LOG_DIR"

TMUX_SOCKET="pipeline_${RUN_TS}_$$"

tmux_cmd() {
  LC_ALL=en_US.UTF-8 tmux -L "$TMUX_SOCKET" "$@"
}

cleanup() {
  echo ""
  echo "Shutting down log sessions..."
  tmux_cmd kill-server 2>/dev/null || true
  {
    echo "Pipeline run: $RUN_TS"
    echo "Monitor only: $MONITOR_ONLY"
    echo "Log directory: $LOG_DIR"
    echo ""
    echo "Files:"
    ls -lh "$LOG_DIR"/ 2>/dev/null || true
    echo ""
    echo "Finished at: $(date)"
  } > "$LOG_DIR/summary.txt"
  echo ""
  echo "========================================================================"
  echo "Pipeline run complete."
  echo ""
  echo "Log files:"
  for f in "$LOG_DIR"/*.log; do
    [ -f "$f" ] && echo "  $f"
  done
  echo ""
  echo "  $LOG_DIR/summary.txt"
  echo "========================================================================"
}
trap cleanup EXIT

# Source .env for UNITY_GCS_ARTIFACT_BUCKET, UNITY_PUBSUB_PROJECT_ID etc.
_ENV_FILE="$REPO_ROOT/.env"
if [ -f "$_ENV_FILE" ]; then
  set -a; . "$_ENV_FILE"; set +a
fi

PUBSUB_PROJECT="${UNITY_PUBSUB_PROJECT_ID:-gcp-project-runtime}"
PARSE_SUB="${UNITY_PARSE_SUB:-unity-parse-sub-staging}"
INGEST_SUB="${UNITY_INGEST_SUB:-unity-ingest-sub-staging}"

echo "========================================================================"
if (( MONITOR_ONLY )); then
  echo "Pipeline Monitor (--monitor)"
else
  echo "Pipeline Runner"
fi
echo "========================================================================"
echo "  Log directory: $LOG_DIR"
echo ""

# ---- 1. Dispatch (skip in monitor mode) ----
if (( ! MONITOR_ONLY )); then
  if (( ${#DISPATCH_ARGS[@]} == 0 )); then
    echo "ERROR: No dispatch arguments provided. Pass --mode, --config, etc."
    echo "       Or use --monitor to only attach to running workers."
    exit 1
  fi

  echo "[1/5] Dispatching pipeline job..."
  cd "$REPO_ROOT"
  uv run unity_deploy/scripts/dispatch_pipeline.py "${DISPATCH_ARGS[@]}" 2>&1 | tee "$LOG_DIR/dispatch.log"
  DISPATCH_EXIT=${PIPESTATUS[0]}

  if [ "$DISPATCH_EXIT" -ne 0 ]; then
    echo "ERROR: dispatch_pipeline.py failed (exit $DISPATCH_EXIT). See $LOG_DIR/dispatch.log"
    exit 1
  fi
  echo "  -> $LOG_DIR/dispatch.log"
  echo ""
else
  echo "[1/5] Skipping dispatch (--monitor mode)"
  echo ""
fi

# ---- 2. Worker log streams ----
echo "[2/5] Starting worker log streams..."

# kubectl logs -f -l ... only attaches to pods that exist at start time.
# HPA-scaled pods are invisible. A reconnecting loop re-attaches every 30s
# so new pods get picked up with minimal delay.
tmux_cmd new-session -d -s "parse-logs" bash -c "
while true; do
  kubectl logs -n $WORKER_NS -l app=unity-parse-worker -f --tail=50 --max-log-requests=20 --prefix=true 2>&1
  echo '[reconnecting to parse workers in 30s...]'
  sleep 30
done | tee '$LOG_DIR/parse-worker.log'
"
echo "  parse-worker.log  (streaming, reconnects every 30s)"

tmux_cmd new-session -d -s "ingest-logs" bash -c "
while true; do
  kubectl logs -n $WORKER_NS -l app=unity-ingest-worker -f --tail=50 --max-log-requests=20 --prefix=true 2>&1
  echo '[reconnecting to ingest workers in 30s...]'
  sleep 30
done | tee '$LOG_DIR/ingest-worker.log'
"
echo "  ingest-worker.log (streaming, reconnects every 30s)"

# ---- 3. HPA & pod monitoring ----
echo ""
echo "[3/5] Starting HPA & pod monitoring..."

tmux_cmd new-session -d -s "hpa-monitor" bash -c "
while true; do
  echo \"--- \$(date +%H:%M:%S) ---\"
  kubectl get hpa -n $WORKER_NS 2>/dev/null
  echo
  sleep 10
done 2>&1 | tee '$LOG_DIR/hpa.log'
"
echo "  hpa.log           (every 10s)"

tmux_cmd new-session -d -s "pod-monitor" bash -c "
while true; do
  echo \"--- \$(date +%H:%M:%S) ---\"
  kubectl get pods -l component=pipeline-worker -n $WORKER_NS 2>/dev/null
  echo
  sleep 10
done 2>&1 | tee '$LOG_DIR/pods.log'
"
echo "  pods.log          (every 10s)"

# ---- 4. Pub/Sub backlog monitoring ----
echo ""
echo "[4/5] Starting Pub/Sub backlog monitoring..."

tmux_cmd new-session -d -s "pubsub-monitor" bash -c "
while true; do
  ts=\$(date +%H:%M:%S)
  parse_backlog=\$(gcloud pubsub subscriptions describe '$PARSE_SUB' \
    --project='$PUBSUB_PROJECT' \
    --format='value(messageRetentionDuration)' 2>/dev/null | head -1)
  # Use the metrics API for actual backlog count
  parse_pending=\$(kubectl get hpa unity-parse-worker-hpa -n $WORKER_NS \
    -o jsonpath='{.status.currentMetrics[0].external.current.averageValue}' 2>/dev/null || echo '?')
  ingest_pending=\$(kubectl get hpa unity-ingest-worker-hpa -n $WORKER_NS \
    -o jsonpath='{.status.currentMetrics[0].external.current.averageValue}' 2>/dev/null || echo '?')
  parse_replicas=\$(kubectl get hpa unity-parse-worker-hpa -n $WORKER_NS \
    -o jsonpath='{.status.currentReplicas}' 2>/dev/null || echo '?')
  ingest_replicas=\$(kubectl get hpa unity-ingest-worker-hpa -n $WORKER_NS \
    -o jsonpath='{.status.currentReplicas}' 2>/dev/null || echo '?')
  printf '%s  parse: backlog=%s replicas=%s  |  ingest: backlog=%s replicas=%s\n' \
    \"\$ts\" \"\$parse_pending\" \"\$parse_replicas\" \"\$ingest_pending\" \"\$ingest_replicas\"
  sleep 15
done 2>&1 | tee '$LOG_DIR/pubsub-backlog.log'
"
echo "  pubsub-backlog.log (every 15s)"

# ---- 5. Summary ----
echo ""
echo "========================================================================"
echo "[5/5] All sessions running. Logs streaming to:"
echo ""
echo "  $LOG_DIR/dispatch.log"
echo "  $LOG_DIR/parse-worker.log"
echo "  $LOG_DIR/ingest-worker.log"
echo "  $LOG_DIR/hpa.log"
echo "  $LOG_DIR/pods.log"
echo "  $LOG_DIR/pubsub-backlog.log"
echo ""
echo "Tail any log:    tail -f $LOG_DIR/ingest-worker.log"
echo "Stop all:        tmux -L $TMUX_SOCKET kill-server"
echo ""
echo "Press Ctrl+C to stop all log streams and generate summary."
echo "========================================================================"

while true; do sleep 60; done
