#!/usr/bin/env bash
# Create/update the adapters Cloud Scheduler jobs and remove the retired legacy
# schedulers + pending PubSub topics. Behaviour is identical to the per-job steps
# that previously lived in cloudbuild/adapters{,-staging}.yaml; the only
# environment difference is the job/topic name suffix ("-staging" vs "").
#
# Usage: configure_adapters_schedulers.sh <env> <region> <service>
#   env: staging | production
# Requires env: ORCHESTRA_ADMIN_KEY.

set -uo pipefail

ENVIRONMENT="${1:?env required}"
REGION="${2:?region required}"
SERVICE="${3:?service required}"
: "${ORCHESTRA_ADMIN_KEY:?ORCHESTRA_ADMIN_KEY required}"

if [ "$ENVIRONMENT" = "staging" ]; then
  SUFFIX="-staging"
else
  SUFFIX=""
fi

CLOUD_RUN_URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
if [ -z "$CLOUD_RUN_URL" ]; then
  echo "Could not resolve Cloud Run URL for $SERVICE" >&2
  exit 1
fi

# upsert_scheduler <name> <schedule> <path> [body] [attempt_deadline]
# When body is provided the job posts JSON; otherwise it posts with no body
# (matching the original per-job step definitions). attempt_deadline (e.g. 540s)
# is applied to jobs whose target may run longer than the Scheduler default
# (180s) -- the token/watch jobs fan out over Orchestra + provider APIs.
upsert_scheduler() {
  local name="$1" schedule="$2" path="$3" body="${4:-}" deadline="${5:-}"
  local uri="${CLOUD_RUN_URL}${path}"
  local -a create_headers update_headers create_extra=() deadline_args=()
  if [ -n "$body" ]; then
    update_headers="Authorization=Bearer ${ORCHESTRA_ADMIN_KEY},Content-Type=application/json"
    create_headers="$update_headers"
    create_extra=(--message-body "$body")
  else
    update_headers="Authorization=Bearer ${ORCHESTRA_ADMIN_KEY}"
    create_headers="$update_headers"
  fi
  if [ -n "$deadline" ]; then
    deadline_args=(--attempt-deadline "$deadline")
  fi
  {
    gcloud scheduler jobs update http "${name}${SUFFIX}" \
      --schedule "$schedule" \
      --uri "$uri" \
      --http-method POST \
      --update-headers "$update_headers" \
      "${create_extra[@]}" \
      "${deadline_args[@]}" \
      --time-zone "UTC" \
      --location="$REGION" \
    || \
    gcloud scheduler jobs create http "${name}${SUFFIX}" \
      --schedule "$schedule" \
      --uri "$uri" \
      --http-method POST \
      --headers "$create_headers" \
      "${create_extra[@]}" \
      "${deadline_args[@]}" \
      --time-zone "UTC" \
      --location="$REGION"
  } 2>&1 | sed "s|${ORCHESTRA_ADMIN_KEY}|[REDACTED]|g"
}

# Sole writer of MICROSOFT_ACCESS_TOKEN at :00/:30; readers below are offset so
# they always see a freshly-rotated token. These four fan out over the fleet
# against Orchestra + provider APIs, so give them a longer attempt deadline than
# the 180s Scheduler default.
upsert_scheduler "microsoft-tokens"      "*/30 * * * *"    "/scheduled/microsoft-tokens" "{}" "540s"
upsert_scheduler "google-token-refresh"  "5-59/30 * * * *" "/scheduled/google-tokens"    "{}" "540s"
upsert_scheduler "teams-watches"         "10-59/30 * * * *" "/scheduled/teams-watches"   "{}" "540s"
upsert_scheduler "email-watches"         "10 0 * * *"      "/scheduled/email-watches"    "{}" "540s"
upsert_scheduler "infra-maintenance"     "0 * * * *"       "/scheduled/infra/maintenance"
upsert_scheduler "cert-renewal"          "0 3 1 * *"       "/scheduled/cert-renewal"

echo "Removing retired legacy infra schedulers..."
for job in jobs-create jobs-cleanup stale-jobs-expire pending-startups pending-vm-assignments; do
  gcloud scheduler jobs delete "${job}${SUFFIX}" --location="$REGION" --quiet 2>/dev/null \
    && echo "Deleted legacy scheduler: ${job}${SUFFIX}" \
    || echo "Legacy scheduler ${job}${SUFFIX} already removed or not found"
done

echo "Removing retired legacy pending PubSub topics..."
for topic in unity-pending-startups unity-pending-vm-assignments unity-startup; do
  gcloud pubsub topics delete "${topic}${SUFFIX}" --project="${PROJECT_ID}" --quiet 2>/dev/null \
    && echo "Deleted legacy topic: ${topic}${SUFFIX}" \
    || echo "Legacy topic ${topic}${SUFFIX} already removed or not found"
done

echo "Adapters scheduler configuration complete."
