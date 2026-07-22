#!/usr/bin/env bash
# Force or graceful restart for one assistant.
#
# Usage:
#   deploy/scripts/dev/restart_assistant.sh <assistant_id> [graceful|force]
set -euo pipefail

ASSISTANT_ID="${1:-}"
MODE="${2:-graceful}"
if [[ -z "${ASSISTANT_ID}" ]]; then
  echo "usage: $0 <assistant_id> [graceful|force]" >&2
  exit 2
fi

COMMS_URL="${UNITY_COMMS_URL:-${COMMS_URL:-https://comms.unify.ai}}"
COMMS_URL="${COMMS_URL%/}"
if [[ -z "${ORCHESTRA_ADMIN_KEY:-}" ]]; then
  echo "ORCHESTRA_ADMIN_KEY must be set" >&2
  exit 2
fi

payload="$(python3 - <<PY
import json
print(json.dumps({
    "mode": "${MODE}",
    "reason": "manual",
    "deadline_seconds": 900,
}))
PY
)"

curl -fsS -X POST "${COMMS_URL}/infra/assistants/${ASSISTANT_ID}/restart" \
  -H "Authorization: Bearer ${ORCHESTRA_ADMIN_KEY}" \
  -H "Content-Type: application/json" \
  -d "${payload}"
echo
