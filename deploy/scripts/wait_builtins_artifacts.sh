#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Wait for async Builtins artifact seeding to finish.

Required:
  --orchestra-url URL
  --admin-key-env ENV_NAME
  --environment staging|production
  --desired-hash HASH

Optional:
  --artifact-kind integrations
  --backend-id BACKEND
  --timeout SECONDS
  --interval SECONDS
USAGE
}

orchestra_url=""
admin_key_env="ORCHESTRA_ADMIN_KEY"
environment=""
artifact_kind="integrations"
backend_id="composio"
desired_hash=""
timeout_seconds="3600"
interval_seconds="15"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --orchestra-url) orchestra_url="$2"; shift 2 ;;
    --admin-key-env) admin_key_env="$2"; shift 2 ;;
    --environment) environment="$2"; shift 2 ;;
    --artifact-kind) artifact_kind="$2"; shift 2 ;;
    --backend-id) backend_id="$2"; shift 2 ;;
    --desired-hash) desired_hash="$2"; shift 2 ;;
    --timeout) timeout_seconds="$2"; shift 2 ;;
    --interval) interval_seconds="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$artifact_kind" != "integrations" ]]; then
  echo "Unsupported Builtins artifact kind for wait gate: ${artifact_kind}" >&2
  exit 2
fi
if [[ -z "$orchestra_url" || -z "$environment" || -z "$backend_id" || -z "$desired_hash" ]]; then
  usage >&2
  exit 2
fi
admin_key="${!admin_key_env:-}"
if [[ -z "$admin_key" ]]; then
  echo "Missing admin key env var: ${admin_key_env}" >&2
  exit 2
fi

deadline=$((SECONDS + timeout_seconds))
url="${orchestra_url%/}/admin/integrations/bootstrap-state?environment=${environment}&backend_id=${backend_id}"
while [[ "$SECONDS" -lt "$deadline" ]]; do
  response="$(curl -fsS -H "Authorization: Bearer ${admin_key}" "$url" || true)"
  if [[ -n "$response" ]]; then
    status="$(python3 -c 'import json,sys; print((json.loads(sys.stdin.read()).get("last_status") or ""))' <<< "$response")"
    hash_value="$(python3 -c 'import json,sys; print((json.loads(sys.stdin.read()).get("desired_hash") or ""))' <<< "$response")"
    if [[ "$hash_value" == "$desired_hash" && "$status" == "success" ]]; then
      python3 - <<PY
import json
print(json.dumps({
    "status": "success",
    "artifact_kind": "$artifact_kind",
    "environment": "$environment",
    "backend_id": "$backend_id",
    "desired_hash": "$desired_hash",
}, sort_keys=True))
PY
      exit 0
    fi
    if [[ "$hash_value" == "$desired_hash" && "$status" == "failed" ]]; then
      echo "$response" >&2
      exit 1
    fi
  fi
  sleep "$interval_seconds"
done

echo "Timed out waiting for Builtins artifact seed ${artifact_kind}/${environment}/${backend_id}/${desired_hash}" >&2
exit 1
