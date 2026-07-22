#!/usr/bin/env bash
# Arm graceful drain/restart for every assistant mapped to a client bundle.
#
# Usage:
#   deploy/scripts/dev/drain_bundle_assistants.sh unify_company production <SHA>
#   deploy/scripts/dev/drain_bundle_assistants.sh unify_company staging <SHA>
#
# Env:
#   UNITY_COMMS_URL / COMMS_URL — Cloud Run defaults below
#   ORCHESTRA_ADMIN_KEY — required (comms /infra admin auth)
set -euo pipefail

BUNDLE_KEY="${1:-}"
ENVIRONMENT="${2:-}"
REVISION="${3:-}"

if [[ -z "${BUNDLE_KEY}" || -z "${ENVIRONMENT}" || -z "${REVISION}" ]]; then
  echo "usage: $0 <bundle_key> {staging|production} <revision_sha>" >&2
  exit 2
fi

COMMS_URL="${UNITY_COMMS_URL:-${COMMS_URL:-}}"
if [[ -z "${COMMS_URL}" ]]; then
  if [[ "${ENVIRONMENT}" == "staging" ]]; then
    COMMS_URL="https://service.a.run.app"
  else
    COMMS_URL="https://service.a.run.app"
  fi
fi
COMMS_URL="${COMMS_URL%/}"

if [[ -z "${ORCHESTRA_ADMIN_KEY:-}" ]]; then
  echo "ORCHESTRA_ADMIN_KEY must be set" >&2
  exit 2
fi

payload="$(python3 - <<PY
import json
print(json.dumps({
    "bundle_key": "${BUNDLE_KEY}",
    "environment": "${ENVIRONMENT}",
    "target_revision": "${REVISION}",
    "mode": "graceful",
    "deadline_seconds": 900,
}))
PY
)"

echo "Arming graceful drain for bundle=${BUNDLE_KEY} env=${ENVIRONMENT} rev=${REVISION}"
curl -fsS -X POST "${COMMS_URL}/infra/assistants/drain-bundle" \
  -H "Authorization: Bearer ${ORCHESTRA_ADMIN_KEY}" \
  -H "Content-Type: application/json" \
  -d "${payload}"
echo
