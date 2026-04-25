#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <pipeline-log-dir|ingest-worker.log>" >&2
}

if [[ $# -ne 1 ]]; then
  usage
  exit 2
fi

INPUT="$1"
if [[ -d "$INPUT" ]]; then
  LOG_FILE="$INPUT/ingest-worker.log"
else
  LOG_FILE="$INPUT"
fi

if [[ ! -f "$LOG_FILE" ]]; then
  echo "ERROR: ingest log not found: $LOG_FILE" >&2
  exit 1
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

completed="$tmpdir/completed.tsv"
starts="$tmpdir/starts.tsv"

rg -o '\[pod/[^]]+\].*Completed job=[A-Za-z0-9_-]+' "$LOG_FILE" \
  | awk '{
      pod=$0; sub(/^.*\[pod\//, "", pod); sub(/\/.*$/, "", pod);
      job=$0; sub(/^.*Completed job=/, "", job); sub(/[, ].*$/, "", job);
      print job "\t" pod
    }' > "$completed" || true

rg -o '\[pod/[^]]+\].*Starting job=[A-Za-z0-9_-]+' "$LOG_FILE" \
  | awk '{
      pod=$0; sub(/^.*\[pod\//, "", pod); sub(/\/.*$/, "", pod);
      job=$0; sub(/^.*Starting job=/, "", job); sub(/[, ].*$/, "", job);
      print job "\t" pod
    }' > "$starts" || true

raw_completed="$(wc -l < "$completed" | tr -d ' ')"
unique_completed="$(cut -f1 "$completed" | sort -u | wc -l | tr -d ' ')"

echo "Ingest log: $LOG_FILE"
echo
echo "Completions"
echo "  raw completed lines:    $raw_completed"
echo "  unique completed jobs:  $unique_completed"
echo

echo "Per-job completion replay multiplicity"
if [[ -s "$completed" ]]; then
  cut -f1 "$completed" | sort | uniq -c | sort -nr \
    | awk '{ printf "  %5d  %s\n", $1, $2 }'
else
  echo "  none"
fi
echo

echo "Per-job pod multiplicity from Starting job lines"
if [[ -s "$starts" ]]; then
  awk '{ pods[$1][$2]=1 } END {
    for (job in pods) {
      count=0
      list=""
      for (pod in pods[job]) {
        count++
        list = list (list ? "," : "") pod
      }
      printf "  %5d  %s  %s\n", count, job, list
    }
  }' "$starts" | sort -nr
else
  echo "  none"
fi
echo

echo "Pods that handled ingest"
awk '{ print $2 }' "$starts" "$completed" 2>/dev/null | sort -u | sed 's/^/  /' || true
echo

echo "Pods that started but never completed"
if [[ -s "$starts" ]]; then
  awk '{ print $2 }' "$starts" | sort -u > "$tmpdir/start_pods"
  awk '{ print $2 }' "$completed" | sort -u > "$tmpdir/completed_pods"
  comm -23 "$tmpdir/start_pods" "$tmpdir/completed_pods" | sed 's/^/  /' || true
else
  echo "  none"
fi
echo

echo "Reconnects and kubectl/log-stream errors"
rg -n 'reconnecting|error:|Error from server|terminated|ContainerStatusUnknown' "$LOG_FILE" \
  | sed 's/^/  /' || echo "  none"
