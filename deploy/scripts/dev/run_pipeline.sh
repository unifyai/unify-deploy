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
#     --env staging \
#     --mode dm \
#     --config path/to/pipeline_config.json \
#     --project-root ~/droid-deploy \
#     --user-id UUID --assistant-id 1823 \
#     [--limit 5] [extra dispatch_pipeline.py flags...]
#
#   # Mixed DM/FM ingestion is intentionally two isolated dispatches: run once
#   # with --mode dm and a DM config, then again with --mode fm and an FM config.
#   # This preserves separate dispatch ids, logs, summaries, and storage semantics.
#
#   # Monitor only (no dispatch — attach to workers already processing)
#   deploy/scripts/dev/run_pipeline.sh --monitor --env staging
#   deploy/scripts/dev/run_pipeline.sh --monitor --env staging --dispatch-id <id>
#
# Creates a timestamped log directory under logs/pipeline/ with:
#   dispatch.log        — dispatch_pipeline.py output (dispatch mode only)
#   parse-worker.log    — streaming kubectl logs for parse pods
#   ingest-worker.log   — streaming kubectl logs for ingest pods
#   hpa.log             — periodic HPA snapshots (every 10s)
#   pods.log            — periodic pod-count snapshots (every 10s)
#   pubsub-backlog.log  — periodic Pub/Sub backlog depth (every 15s)
#   dlq.log             — periodic DLQ backlog depth (every 60s)
#   dispatch-status.log — dispatch/job derived status, checkpoints, DLQ, retry plan
#   job-progress.log    — dispatch-correlated derived status snapshots
#   dlq-matches.log     — dispatch-correlated DLQ snapshots
#   retryable-failures.log — retryability snapshots for failed/DLQ jobs
#   summary.json        — machine-readable final dispatch status when dispatch id is known
#   summary.txt         — final run summary
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"

# ---- Parse our own flags before forwarding the rest ----
MONITOR_ONLY=0
PIPELINE_ENV_ARG=""
DISPATCH_ID="${DISPATCH_ID:-}"
DISPATCH_ARGS=()
while (( $# > 0 )); do
  case "$1" in
    --monitor)
      MONITOR_ONLY=1
      shift
      ;;
    --env)
      PIPELINE_ENV_ARG="${2:-}"
      if [[ -z "$PIPELINE_ENV_ARG" ]]; then
        echo "ERROR: --env requires 'staging' or 'production'." >&2
        exit 2
      fi
      shift 2
      ;;
    --env=*)
      PIPELINE_ENV_ARG="${1#--env=}"
      shift
      ;;
    --dispatch-id)
      DISPATCH_ID="${2:-}"
      if [[ -z "$DISPATCH_ID" ]]; then
        echo "ERROR: --dispatch-id requires a dispatch id." >&2
        exit 2
      fi
      shift 2
      ;;
    --dispatch-id=*)
      DISPATCH_ID="${1#--dispatch-id=}"
      shift
      ;;
    *)
      DISPATCH_ARGS+=("$1")
      shift
      ;;
  esac
done

RUN_TS="$(date +%Y-%m-%dT%H-%M-%S)"
RUN_START_RFC3339="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
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
    echo "Environment: ${PIPELINE_ENV:-unknown}"
    echo "Monitor only: $MONITOR_ONLY"
    echo "Log directory: $LOG_DIR"
    if [[ -n "${DISPATCH_ID:-}" ]]; then
      echo "Dispatch ID: $DISPATCH_ID"
      echo ""
      echo "Final dispatch status JSON:"
      (
        cd "$REPO_ROOT"
        uv run python -m droid_deploy.infra.cli.pipeline_control status \
          --env "$PIPELINE_ENV" \
          --dispatch-id "$DISPATCH_ID" \
          --json > "$LOG_DIR/summary.json"
      ) 2>/dev/null || true
      [ -f "$LOG_DIR/summary.json" ] && echo "  $LOG_DIR/summary.json"
    fi
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

# Source .env for DROID_GCS_ARTIFACT_BUCKET, DROID_PUBSUB_PROJECT_ID etc.
PRESET_WORKER_NS="${DROID_WORKER_NS:-}"
PRESET_PARSE_SUB="${DROID_PARSE_SUB:-}"
PRESET_INGEST_SUB="${DROID_INGEST_SUB:-}"
PRESET_DLQ_SUB="${DROID_DLQ_SUB:-}"
PRESET_ARTIFACT_BUCKET="${DROID_GCS_ARTIFACT_BUCKET:-}"
_ENV_FILE="$REPO_ROOT/.env"
if [ -f "$_ENV_FILE" ]; then
  set -a; . "$_ENV_FILE"; set +a
fi

if [[ -n "$PIPELINE_ENV_ARG" ]]; then
  PIPELINE_ENV="$PIPELINE_ENV_ARG"
else
  PIPELINE_ENV="${DROID_GCP_PIPELINE_ENVIRONMENT:-staging}"
fi

case "$PIPELINE_ENV" in
  staging) ENV_SUFFIX="-staging" ;;
  production) ENV_SUFFIX="" ;;
  *)
    echo "ERROR: --env must be 'staging' or 'production' (got '$PIPELINE_ENV')." >&2
    exit 2
    ;;
esac

PUBSUB_PROJECT="${DROID_PUBSUB_PROJECT_ID:-gcp-project-runtime}"
if [[ -n "$PIPELINE_ENV_ARG" ]]; then
  WORKER_NS="${PRESET_WORKER_NS:-$PIPELINE_ENV}"
  PARSE_SUB="${PRESET_PARSE_SUB:-droid-parse-sub${ENV_SUFFIX}}"
  INGEST_SUB="${PRESET_INGEST_SUB:-droid-ingest-sub${ENV_SUFFIX}}"
  DLQ_SUB="${PRESET_DLQ_SUB:-droid-dead-letter-sub${ENV_SUFFIX}}"
  ARTIFACT_BUCKET="${PRESET_ARTIFACT_BUCKET:-droid-pipeline-artifacts${ENV_SUFFIX}}"
else
  WORKER_NS="${DROID_WORKER_NS:-$PIPELINE_ENV}"
  PARSE_SUB="${DROID_PARSE_SUB:-droid-parse-sub${ENV_SUFFIX}}"
  INGEST_SUB="${DROID_INGEST_SUB:-droid-ingest-sub${ENV_SUFFIX}}"
  DLQ_SUB="${DROID_DLQ_SUB:-droid-dead-letter-sub${ENV_SUFFIX}}"
  ARTIFACT_BUCKET="${DROID_GCS_ARTIFACT_BUCKET:-droid-pipeline-artifacts${ENV_SUFFIX}}"
fi
export DROID_GCP_PIPELINE_ENVIRONMENT="$PIPELINE_ENV"
export DROID_GCS_ARTIFACT_BUCKET="$ARTIFACT_BUCKET"

echo "========================================================================"
if (( MONITOR_ONLY )); then
  echo "Pipeline Monitor (--monitor)"
else
  echo "Pipeline Runner"
fi
echo "========================================================================"
echo "  Environment:   $PIPELINE_ENV"
echo "  Worker ns:     $WORKER_NS"
echo "  Artifact GCS:  gs://$ARTIFACT_BUCKET"
echo "  Pub/Sub project: $PUBSUB_PROJECT"
echo "  Parse sub:     $PARSE_SUB"
echo "  Ingest sub:    $INGEST_SUB"
echo "  DLQ sub:       $DLQ_SUB"
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
  uv run droid_deploy/scripts/dispatch_pipeline.py "${DISPATCH_ARGS[@]}" 2>&1 | tee "$LOG_DIR/dispatch.log"
  DISPATCH_EXIT=${PIPESTATUS[0]}

  if [ "$DISPATCH_EXIT" -ne 0 ]; then
    echo "ERROR: dispatch_pipeline.py failed (exit $DISPATCH_EXIT). See $LOG_DIR/dispatch.log"
    exit 1
  fi
  echo "  -> $LOG_DIR/dispatch.log"
  DISPATCH_ID="$(python3 - "$LOG_DIR/dispatch.log" <<'PY'
import re
import sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
matches = re.findall(r"Dispatch ([0-9a-f]{32})", text)
print(matches[-1] if matches else "")
PY
)"
  if [[ -n "$DISPATCH_ID" ]]; then
    echo "  Dispatch ID: $DISPATCH_ID"
  else
    echo "  WARNING: could not infer dispatch id from dispatch.log"
  fi
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
  kubectl logs -n $WORKER_NS -l app=droid-parse-worker -f --since-time='$RUN_START_RFC3339' --max-log-requests=50 --prefix=true 2>&1
  echo '[reconnecting to parse workers in 30s...]'
  sleep 30
done | awk '!seen[\$0]++' | tee '$LOG_DIR/parse-worker.log'
"
echo "  parse-worker.log  (streaming, reconnects every 30s)"

tmux_cmd new-session -d -s "ingest-logs" bash -c "
while true; do
  kubectl logs -n $WORKER_NS -l app=droid-ingest-worker -f --since-time='$RUN_START_RFC3339' --max-log-requests=50 --prefix=true 2>&1
  echo '[reconnecting to ingest workers in 30s...]'
  sleep 30
done | awk '!seen[\$0]++' | tee '$LOG_DIR/ingest-worker.log'
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
metric_value() {
  local sub=\"\$1\"
  local metric=\"\$2\"
  local token end start
  token=\$(gcloud auth print-access-token 2>/dev/null) || { echo '?'; return; }
  end=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
  start=\$(date -u -d '2 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
  curl -fsG -H \"Authorization: Bearer \$token\" \
    --data-urlencode \"filter=metric.type=\\\"pubsub.googleapis.com/subscription/\$metric\\\" AND resource.label.subscription_id=\\\"\$sub\\\"\" \
    --data-urlencode \"interval.endTime=\$end\" \
    --data-urlencode \"interval.startTime=\$start\" \
    \"https://monitoring.googleapis.com/v3/projects/$PUBSUB_PROJECT/timeSeries\" \
    | python3 -c \"import json,sys; d=json.load(sys.stdin); ts=d.get('timeSeries') or []; points=(ts[0].get('points') or []) if ts else []; value=(points[0].get('value') or {}) if points else {}; print(value.get('int64Value') or value.get('doubleValue') or '?')\" 2>/dev/null || echo '?'
}
while true; do
  ts=\$(date +%H:%M:%S)
  parse_undeliv=\$(metric_value '$PARSE_SUB' 'num_undelivered_messages')
  ingest_undeliv=\$(metric_value '$INGEST_SUB' 'num_undelivered_messages')
  parse_oldest=\$(metric_value '$PARSE_SUB' 'oldest_unacked_message_age')
  ingest_oldest=\$(metric_value '$INGEST_SUB' 'oldest_unacked_message_age')
  parse_replicas=\$(kubectl get hpa droid-parse-worker-hpa -n $WORKER_NS \
    -o jsonpath='{.status.currentReplicas}/{.status.desiredReplicas}' 2>/dev/null || echo '?')
  ingest_replicas=\$(kubectl get hpa droid-ingest-worker-hpa -n $WORKER_NS \
    -o jsonpath='{.status.currentReplicas}/{.status.desiredReplicas}' 2>/dev/null || echo '?')
  printf '%s  parse: undeliv=%s oldest=%ss replicas=%s  |  ingest: undeliv=%s oldest=%ss replicas=%s\n' \
    \"\$ts\" \"\$parse_undeliv\" \"\$parse_oldest\" \"\$parse_replicas\" \"\$ingest_undeliv\" \"\$ingest_oldest\" \"\$ingest_replicas\"
  sleep 15
done 2>&1 | tee '$LOG_DIR/pubsub-backlog.log'
"
echo "  pubsub-backlog.log (every 15s)"

tmux_cmd new-session -d -s "dlq-monitor" bash -c "
metric_value() {
  local sub=\"\$1\"
  local token end start
  token=\$(gcloud auth print-access-token 2>/dev/null) || { echo '?'; return; }
  end=\$(date -u +%Y-%m-%dT%H:%M:%SZ)
  start=\$(date -u -d '2 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
  curl -fsG -H \"Authorization: Bearer \$token\" \
    --data-urlencode \"filter=metric.type=\\\"pubsub.googleapis.com/subscription/num_undelivered_messages\\\" AND resource.label.subscription_id=\\\"\$sub\\\"\" \
    --data-urlencode \"interval.endTime=\$end\" \
    --data-urlencode \"interval.startTime=\$start\" \
    \"https://monitoring.googleapis.com/v3/projects/$PUBSUB_PROJECT/timeSeries\" \
    | python3 -c \"import json,sys; d=json.load(sys.stdin); ts=d.get('timeSeries') or []; points=(ts[0].get('points') or []) if ts else []; value=(points[0].get('value') or {}) if points else {}; print(value.get('int64Value') or value.get('doubleValue') or '?')\" 2>/dev/null || echo '?'
}
while true; do
  ts=\$(date +%H:%M:%S)
  dlq_count=\$(metric_value '$DLQ_SUB')
  printf '%s  %s: undeliv=%s\n' \"\$ts\" '$DLQ_SUB' \"\${dlq_count:-?}\"
  sleep 60
done 2>&1 | tee '$LOG_DIR/dlq.log'
"
echo "  dlq.log            (every 60s)"

if [[ -n "$DISPATCH_ID" ]]; then
  tmux_cmd new-session -d -s "dispatch-status" bash -c "
while true; do
  cd '$REPO_ROOT'
  echo \"--- \$(date +%H:%M:%S) dispatch=$DISPATCH_ID ---\"
  uv run python -m droid_deploy.infra.cli.pipeline_control status \
    --env '$PIPELINE_ENV' \
    --dispatch-id '$DISPATCH_ID' \
    --show-checkpoints \
    --show-dlq \
    --show-retry-plan 2>&1
  echo
  uv run python -m droid_deploy.infra.cli.pipeline_control status \
    --env '$PIPELINE_ENV' \
    --dispatch-id '$DISPATCH_ID' \
    --json > '$LOG_DIR/summary.json' 2>/dev/null || true
  python3 - '$LOG_DIR/summary.json' <<'PY' 2>/dev/null | tee -a '$LOG_DIR/job-progress.log' '$LOG_DIR/dlq-matches.log' '$LOG_DIR/retryable-failures.log' >/dev/null || true
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding='utf-8'))
jobs = data.get('jobs', [])
print('--- status snapshot ---')
for job in jobs:
    print(f\"{job.get('job_id')} derived={job.get('derived_status')} rows={job.get('latest_checkpoint_rows')} chunks={job.get('latest_checkpoint_chunks')} queue={job.get('queue_location')} retry={job.get('retry_eligible')}\")
PY
  sleep 60
done 2>&1 | tee '$LOG_DIR/dispatch-status.log'
"
  echo "  dispatch-status.log (every 60s, includes checkpoints/DLQ/retry plan)"
  echo "  job-progress.log    (derived status snapshots)"
  echo "  dlq-matches.log     (dispatch-correlated DLQ snapshots)"
  echo "  retryable-failures.log"
fi

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
echo "  $LOG_DIR/dlq.log"
if [[ -n "$DISPATCH_ID" ]]; then
  echo "  $LOG_DIR/dispatch-status.log"
  echo "  $LOG_DIR/job-progress.log"
  echo "  $LOG_DIR/dlq-matches.log"
  echo "  $LOG_DIR/retryable-failures.log"
  echo "  $LOG_DIR/summary.json"
fi
echo ""
echo "Tail any log:    tail -f $LOG_DIR/ingest-worker.log"
echo "Stop all:        tmux -L $TMUX_SOCKET kill-server"
echo ""
echo "Press Ctrl+C to stop all log streams and generate summary."
echo "========================================================================"

while true; do sleep 60; done
