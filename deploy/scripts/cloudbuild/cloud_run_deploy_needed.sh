#!/usr/bin/env bash
# Decide whether a Cloud Run service needs redeploying by comparing the freshly
# built+pushed image digest against the digest the service currently serves.
# This is the digest-gate that lets the orchestrator skip an unchanged service's
# deploy (and its follow-on work) instead of minting an identical revision.
#
# Prints exactly "changed" or "unchanged" on stdout. Fail-safe: on any
# uncertainty (service/image lookup fails) it prints "changed" so we never skip a
# deploy we should have run.
#
# Usage: cloud_run_deploy_needed.sh <service> <region> <image_ref_with_tag>

set -uo pipefail

SERVICE="${1:?service required}"
REGION="${2:?region required}"
IMAGE_REF="${3:?image ref required}"

new_digest="$(gcloud artifacts docker images describe "$IMAGE_REF" \
  --format='value(image_summary.digest)' 2>/dev/null)" || new_digest=""
if [ -z "$new_digest" ]; then
  echo "changed"
  exit 0
fi

service_json="$(gcloud run services describe "$SERVICE" \
  --region="$REGION" --platform=managed --format=json 2>/dev/null)" || service_json=""
if [ -z "$service_json" ]; then
  # Service does not exist yet -> must deploy.
  echo "changed"
  exit 0
fi

live_digest="$(printf '%s' "$service_json" | python3 -c '
import json, sys
svc = json.load(sys.stdin)
traffic = svc.get("status", {}).get("traffic", [])
# Prefer the revision currently serving the most traffic; fall back to latest.
best = None
for entry in traffic:
    pct = entry.get("percent") or 0
    if best is None or pct > (best.get("percent") or 0):
        best = entry
rev = (best or {}).get("revisionName") or svc.get("status", {}).get("latestReadyRevisionName", "")
print(rev)
' 2>/dev/null)"

if [ -z "$live_digest" ]; then
  echo "changed"
  exit 0
fi

# live_digest currently holds a revision name; resolve its image digest.
serving_digest="$(gcloud run revisions describe "$live_digest" \
  --region="$REGION" --format='value(status.imageDigest)' 2>/dev/null)" || serving_digest=""

if [ -z "$serving_digest" ]; then
  echo "changed"
  exit 0
fi

# status.imageDigest may be a bare sha256:... or a full ref@sha256:...; compare
# on the sha suffix.
new_sha="${new_digest##*@}"
new_sha="${new_sha##*:}"
serving_sha="${serving_digest##*@}"
serving_sha="${serving_sha##*:}"

if [ -n "$new_sha" ] && [ "$new_sha" = "$serving_sha" ]; then
  echo "unchanged"
else
  echo "changed"
fi
exit 0
