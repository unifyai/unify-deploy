#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"

ACK=0
LIMIT=100
SUB="${UNITY_DLQ_SUB:-unity-dead-letter-sub-staging}"
PROJECT="${UNITY_PUBSUB_PROJECT_ID:-gcp-project-runtime}"

usage() {
  cat >&2 <<'EOF'
Usage: drain_dlq.sh [--ack] [--subscription NAME] [--project PROJECT] [--limit N]

Dry-run is the default: messages are pulled and decoded, but not acked.
Pass --ack to acknowledge messages after writing them to logs/dlq/.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ack) ACK=1; shift ;;
    --subscription) SUB="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

mkdir -p "$REPO_ROOT/logs/dlq"
ts="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
raw_file="$(mktemp)"
out_file="$REPO_ROOT/logs/dlq/${ts}.jsonl"
trap 'rm -f "$raw_file"' EXIT

pull_args=("$SUB" "--project=$PROJECT" "--limit=$LIMIT" "--format=json")
if (( ACK )); then
  pull_args+=("--auto-ack")
fi

gcloud pubsub subscriptions pull "${pull_args[@]}" > "$raw_file"

python3 - "$raw_file" "$out_file" "$SUB" "$PROJECT" "$ACK" <<'PY'
import base64
import json
import sys
from datetime import datetime, timezone

raw_path, out_path, sub, project, ack = sys.argv[1:]
with open(raw_path, "r", encoding="utf-8") as f:
    messages = json.load(f)

with open(out_path, "w", encoding="utf-8") as out:
    for item in messages:
        msg = item.get("message", {})
        data = msg.get("data") or ""
        decoded = data
        try:
            decoded = base64.b64decode(data, validate=True).decode("utf-8")
        except Exception:
            pass
        record = {
            "drained_at": datetime.now(timezone.utc).isoformat(),
            "subscription": sub,
            "project": project,
            "acked": ack == "1",
            "ack_id": item.get("ackId"),
            "message_id": msg.get("messageId"),
            "publish_time": msg.get("publishTime"),
            "attributes": msg.get("attributes") or {},
            "payload": decoded,
        }
        try:
            record["payload_json"] = json.loads(decoded)
        except Exception:
            pass
        out.write(json.dumps(record, ensure_ascii=False) + "\n")

print(len(messages))
PY

count="$(wc -l < "$out_file" | tr -d ' ')"
mode="dry-run"
if (( ACK )); then
  mode="acked"
fi

echo "Wrote $count DLQ message(s) to $out_file ($mode)."
