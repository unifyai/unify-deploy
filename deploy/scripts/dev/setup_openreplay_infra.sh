#!/usr/bin/env bash
set -euo pipefail
#
# Provision GCS buckets + service account for self-hosted OpenReplay.
#
# Usage:
#   ./deploy/scripts/dev/setup_openreplay_infra.sh [staging|production]
#
# Creates (idempotent):
#   - gs://bucket{recordings,assets,sourcemaps}[-staging]
#   - SA openreplay-gcs@PROJECT with objectAdmin on those buckets
#   - HMAC key for S3-compatible GCS access (printed once; store in Secret Manager)
#
# Does NOT install OpenReplay itself — see deploy/k8s/openreplay/README.md
# for the GCE VM + openreplay-cli steps and vars.yaml s3 block.
#

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"

ENV="${1:-staging}"
PROJECT_ID="${UNITY_OPENREPLAY_PROJECT_ID:-gcp-project-runtime}"
REGION="us-central1"

if [[ "${ENV}" == "production" ]]; then
  SUFFIX=""
else
  SUFFIX="-${ENV}"
fi

SA_NAME="openreplay-gcs"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

BUCKETS=(
  "unity-openreplay-recordings${SUFFIX}"
  "unity-openreplay-assets${SUFFIX}"
  "unity-openreplay-sourcemaps${SUFFIX}"
)

echo "=== OpenReplay GCS setup (env=${ENV}) ==="
echo "Project: ${PROJECT_ID}"
echo ""

echo "Ensuring service account ${SA_EMAIL}..."
gcloud iam service-accounts create "${SA_NAME}" \
  --project="${PROJECT_ID}" \
  --display-name="OpenReplay GCS (S3 interop)" \
  2>/dev/null || echo "  (service account already exists)"

for BUCKET in "${BUCKETS[@]}"; do
  echo "Creating GCS bucket: ${BUCKET}"
  gsutil mb -p "${PROJECT_ID}" -l "${REGION}" "gs://${BUCKET}" 2>/dev/null \
    || echo "  (bucket already exists)"
  echo "Applying 30-day lifecycle..."
  gsutil lifecycle set \
    "${REPO_ROOT}/deploy/k8s/openreplay/gcs-lifecycle-30d.json" \
    "gs://${BUCKET}"
  echo "Granting objectAdmin to ${SA_EMAIL} on ${BUCKET}..."
  gsutil iam ch \
    "serviceAccount:${SA_EMAIL}:roles/storage.objectAdmin" \
    "gs://${BUCKET}"
done

# CORS so the OpenReplay frontend / tracker can fetch cached assets.
CORS_TMP="$(mktemp)"
cat >"${CORS_TMP}" <<'EOF'
[
  {
    "origin": ["https://*.unify.ai", "https://console.unify.ai", "https://internal.example.com", "http://localhost:3000"],
    "method": ["GET", "HEAD", "PUT", "POST"],
    "responseHeader": ["Content-Type", "Access-Control-Allow-Origin"],
    "maxAgeSeconds": 3600
  }
]
EOF
for BUCKET in "${BUCKETS[@]}"; do
  echo "Setting CORS on ${BUCKET}..."
  gsutil cors set "${CORS_TMP}" "gs://${BUCKET}"
done
rm -f "${CORS_TMP}"

echo ""
echo "=== HMAC key for S3-compatible access ==="
echo "OpenReplay talks to GCS via the S3 API. Create (or reuse) an HMAC key:"
echo ""
echo "  gcloud storage hmac create ${SA_EMAIL} --project=${PROJECT_ID}"
echo ""
echo "Store accessId + secret in Secret Manager, e.g.:"
echo "  OPENREPLAY_GCS_HMAC_ACCESS_ID${SUFFIX}"
echo "  OPENREPLAY_GCS_HMAC_SECRET${SUFFIX}"
echo ""
echo "Then put them in /var/lib/openreplay/vars.yaml under s3: (see"
echo "deploy/k8s/openreplay/vars.yaml.example)."
echo ""
echo "Buckets ready:"
for BUCKET in "${BUCKETS[@]}"; do
  echo "  gs://${BUCKET}"
done
