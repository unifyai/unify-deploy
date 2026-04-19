#!/usr/bin/env bash
set -euo pipefail
#
# Set up GCP infrastructure for pipeline workers.
#
# Usage:
#   ./deploy/scripts/dev/setup_pipeline_infra.sh [staging|production]
#
# Prerequisites:
#   - gcloud CLI authenticated with appropriate project
#   - Permissions: pubsub.topics.create, pubsub.subscriptions.create, storage.buckets.create
#

ENV="${1:-staging}"
PROJECT_ID="${UNITY_PUBSUB_PROJECT_ID:-gcp-project-runtime}"
REGION="us-central1"

# Name suffix matches the unity/communication convention:
# production resources have no suffix; all other envs suffix with "-${ENV}".
if [[ "${ENV}" == "production" ]]; then
  SUFFIX=""
else
  SUFFIX="-${ENV}"
fi

echo "=== Pipeline Infrastructure Setup (env=${ENV}) ==="
echo "Project: ${PROJECT_ID}"
echo "Name suffix: '${SUFFIX}'"
echo ""

# --- GCS Buckets ---
BUCKET="unity-pipeline-artifacts${SUFFIX}"
echo "Creating GCS bucket: ${BUCKET}"
gsutil mb -p "${PROJECT_ID}" -l "${REGION}" "gs://${BUCKET}" 2>/dev/null || echo "  (bucket already exists)"

echo "Applying lifecycle rules..."
gsutil lifecycle set deploy/k8s/workers/gcs-lifecycle-rules.json "gs://${BUCKET}"

# --- Pub/Sub Topics ---
TOPICS=("unity-parse${SUFFIX}" "unity-ingest${SUFFIX}" "unity-dead-letter${SUFFIX}")
for TOPIC in "${TOPICS[@]}"; do
  echo "Creating topic: ${TOPIC}"
  gcloud pubsub topics create "${TOPIC}" --project="${PROJECT_ID}" 2>/dev/null || echo "  (topic already exists)"
done

# --- Pub/Sub Subscriptions ---
echo "Creating subscription: unity-parse-sub${SUFFIX}"
gcloud pubsub subscriptions create "unity-parse-sub${SUFFIX}" \
  --topic="unity-parse${SUFFIX}" \
  --project="${PROJECT_ID}" \
  --ack-deadline=600 \
  --message-retention-duration=7d \
  --dead-letter-topic="unity-dead-letter${SUFFIX}" \
  --max-delivery-attempts=5 \
  2>/dev/null || echo "  (subscription already exists)"

echo "Creating subscription: unity-ingest-sub${SUFFIX}"
gcloud pubsub subscriptions create "unity-ingest-sub${SUFFIX}" \
  --topic="unity-ingest${SUFFIX}" \
  --project="${PROJECT_ID}" \
  --ack-deadline=600 \
  --message-retention-duration=7d \
  --dead-letter-topic="unity-dead-letter${SUFFIX}" \
  --max-delivery-attempts=5 \
  2>/dev/null || echo "  (subscription already exists)"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "To test locally against this infrastructure:"
echo ""
echo "  # GCP settings (both workers)"
echo "  export UNITY_GCP_PIPELINE_ENVIRONMENT=${ENV}"
echo "  export UNITY_GCS_ARTIFACT_BUCKET=${BUCKET}"
echo "  export UNITY_PUBSUB_PROJECT_ID=${PROJECT_ID}"
echo ""
echo "  # Unify identity (ingest worker only — parse worker does not need these)"
echo "  export UNIFY_KEY=<your-unify-key>"
echo "  export USER_ID=<user-id>"
echo "  export ASSISTANT_ID=<assistant-agent-id>"
echo ""
echo "  # Terminal 1: Parse worker (no Unify SDK — GCS + Pub/Sub only)"
echo "  python -m unity_deploy.infra.workers.entrypoint_parse"
echo ""
echo "  # Terminal 2: Ingest worker (needs UNIFY_KEY, USER_ID, ASSISTANT_ID)"
echo "  python -m unity_deploy.infra.workers.entrypoint_ingest --project Assistants"
echo ""
echo "  # Terminal 3: Submit a job"
echo "  python -m unity_deploy.infra.cli.pipeline_control submit \\"
echo "    --config path/to/pipeline_config.json --project MyProject"
echo ""
echo "  # Monitor / cancel / inspect"
echo "  python -m unity_deploy.infra.cli.pipeline_control monitor --job-id <id> --follow"
echo "  python -m unity_deploy.infra.cli.pipeline_control cancel --job-id <id>"
echo "  python -m unity_deploy.infra.cli.pipeline_control inspect --job-id <id>"
