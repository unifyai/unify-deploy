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
CLUSTER="${UNITY_GKE_CLUSTER:-unity}"

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
  --max-delivery-attempts=15 \
  --expiration-period=never \
  2>/dev/null || echo "  (subscription already exists)"

echo "Creating subscription: unity-ingest-sub${SUFFIX}"
gcloud pubsub subscriptions create "unity-ingest-sub${SUFFIX}" \
  --topic="unity-ingest${SUFFIX}" \
  --project="${PROJECT_ID}" \
  --ack-deadline=600 \
  --message-retention-duration=7d \
  --dead-letter-topic="unity-dead-letter${SUFFIX}" \
  --max-delivery-attempts=15 \
  --expiration-period=never \
  2>/dev/null || echo "  (subscription already exists)"

echo "Creating subscription: unity-dead-letter-sub${SUFFIX}"
gcloud pubsub subscriptions create "unity-dead-letter-sub${SUFFIX}" \
  --topic="unity-dead-letter${SUFFIX}" \
  --project="${PROJECT_ID}" \
  --ack-deadline=600 \
  --message-retention-duration=31d \
  --expiration-period=never \
  2>/dev/null || echo "  (subscription already exists)"

# --- Custom Metrics Stackdriver Adapter (for HPAs on Pub/Sub backlog) ---
#
# The parse/ingest worker HPAs scale on the external metric
# `pubsub.googleapis.com|subscription|num_undelivered_messages`. GKE does
# not serve that metric out of the box -- it requires Google's
# Custom Metrics Stackdriver Adapter, which proxies Cloud Monitoring into
# Kubernetes' `external.metrics.k8s.io` API. Without it every HPA reports
# `FailedGetExternalMetric` and the workers never scale past `minReplicas`.
#
# This block is cluster-scoped (not per-env), idempotent, and assumes the
# cluster has Workload Identity enabled (it is, on `${CLUSTER}`). The
# adapter's KSA is bound to a least-privileged GSA with `monitoring.viewer`.
ADAPTER_GSA="custom-metrics-adapter"
ADAPTER_GSA_EMAIL="${ADAPTER_GSA}@${PROJECT_ID}.iam.gserviceaccount.com"
ADAPTER_KSA_BINDING="serviceAccount:${PROJECT_ID}.svc.id.goog[custom-metrics/custom-metrics-stackdriver-adapter]"
ADAPTER_MANIFEST_URL="https://raw.githubusercontent.com/GoogleCloudPlatform/k8s-stackdriver/master/custom-metrics-stackdriver-adapter/deploy/production/adapter_new_resource_model.yaml"

echo ""
echo "=== External Metrics Adapter (cluster=${CLUSTER}) ==="

echo "Fetching cluster credentials..."
gcloud container clusters get-credentials "${CLUSTER}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" >/dev/null

# --- Kubernetes Namespaces ---
# Workers live in env-scoped namespaces (staging / production) alongside
# the other workloads for that environment. Ensure the target namespace
# exists before we apply worker manifests or the adapter.
WORKER_NS="${ENV}"
echo "Ensuring namespace '${WORKER_NS}' exists..."
kubectl create namespace "${WORKER_NS}" --dry-run=client -o yaml | kubectl apply -f -

# Worker manifests reference `unity-secrets` for ORCHESTRA_ADMIN_KEY and
# GCP_SA_KEY. Secrets are namespace-scoped, so if the target namespace
# doesn't have the key yet, copy it from `default`.
if ! kubectl get secret unity-secrets -n "${WORKER_NS}" -o jsonpath='{.data.GCP_SA_KEY}' >/dev/null 2>&1; then
  echo "Copying GCP_SA_KEY from default namespace into ${WORKER_NS}..."
  GCP_SA_KEY=$(kubectl get secret unity-secrets -n default -o jsonpath='{.data.GCP_SA_KEY}')
  if [ -n "${GCP_SA_KEY}" ]; then
    kubectl patch secret unity-secrets -n "${WORKER_NS}" \
      --type='json' \
      -p="[{\"op\":\"add\",\"path\":\"/data/GCP_SA_KEY\",\"value\":\"${GCP_SA_KEY}\"}]"
  else
    echo "  WARNING: GCP_SA_KEY not found in default namespace either"
  fi
else
  echo "  (GCP_SA_KEY already present in ${WORKER_NS}/unity-secrets)"
fi

if kubectl get ns custom-metrics >/dev/null 2>&1; then
  echo "  (custom-metrics namespace already present -- re-applying to pick up manifest drift)"
fi
kubectl apply -f "${ADAPTER_MANIFEST_URL}"

if ! gcloud iam service-accounts describe "${ADAPTER_GSA_EMAIL}" \
    --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "Creating GSA ${ADAPTER_GSA_EMAIL}..."
  gcloud iam service-accounts create "${ADAPTER_GSA}" \
    --project="${PROJECT_ID}" \
    --display-name="Custom Metrics Stackdriver Adapter"
else
  echo "  (GSA ${ADAPTER_GSA_EMAIL} already exists)"
fi

echo "Granting roles/monitoring.viewer to ${ADAPTER_GSA_EMAIL}..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${ADAPTER_GSA_EMAIL}" \
  --role="roles/monitoring.viewer" \
  --condition=None >/dev/null

echo "Binding adapter KSA -> GSA via Workload Identity..."
gcloud iam service-accounts add-iam-policy-binding "${ADAPTER_GSA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="${ADAPTER_KSA_BINDING}" \
  --condition=None >/dev/null

kubectl annotate serviceaccount \
  --namespace=custom-metrics \
  custom-metrics-stackdriver-adapter \
  "iam.gke.io/gcp-service-account=${ADAPTER_GSA_EMAIL}" --overwrite >/dev/null

kubectl rollout restart deployment -n custom-metrics custom-metrics-stackdriver-adapter >/dev/null
kubectl rollout status  deployment -n custom-metrics custom-metrics-stackdriver-adapter --timeout=120s

echo "Adapter ready. Verifying external metrics API..."
kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1" >/dev/null \
  && echo "  external.metrics.k8s.io/v1beta1 is being served"

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
