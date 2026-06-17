#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Ensure minimal hosted infra for async Builtins artifact seeding.

Required:
  --environment staging|production

Optional:
  --job-name JOB
  --gcp-project PROJECT
  --source-service ORCHESTRA_CLOUD_RUN_SERVICE
  --region REGION
  --request-bucket BUCKET
  --dry-run

This creates the shared GCS request bucket when missing and grants the Orchestra
Cloud Run service account access to read artifact seed requests.
USAGE
}

job_name=""
gcp_project=""
environment=""
source_service=""
region=""
request_bucket=""
dry_run="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment) environment="$2"; shift 2 ;;
    --job-name) job_name="$2"; shift 2 ;;
    --gcp-project) gcp_project="$2"; shift 2 ;;
    --source-service) source_service="$2"; shift 2 ;;
    --region) region="$2"; shift 2 ;;
    --request-bucket) request_bucket="$2"; shift 2 ;;
    --dry-run) dry_run="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$environment" ]]; then
  usage >&2
  exit 2
fi
case "$environment" in
  staging)
    source_service="${source_service:-orchestra-staging}"
    job_name="${job_name:-seed-builtins-artifacts-integrations-staging}"
    ;;
  production)
    source_service="${source_service:-orchestra}"
    job_name="${job_name:-seed-builtins-artifacts-integrations-production}"
    ;;
  *) echo "Unsupported environment: ${environment}" >&2; exit 2 ;;
esac
gcp_project="${gcp_project:-gcp-project-saas}"
region="${region:-europe-west1}"
request_bucket="${request_bucket:-${gcp_project}-builtins-artifacts-requests}"

gcloud_cmd=(gcloud)
if [[ -n "$gcp_project" ]]; then
  gcloud_cmd+=(--project "$gcp_project")
fi

if [[ "$dry_run" == "true" ]]; then
  python3 - <<PY
import json
print(json.dumps({
    "status": "dry_run",
    "environment": "$environment",
    "job_name": "$job_name",
    "gcp_project": "$gcp_project",
    "source_service": "$source_service",
    "region": "$region",
    "request_bucket": "$request_bucket",
}, sort_keys=True))
PY
  exit 0
fi

"${gcloud_cmd[@]}" run services describe "$source_service" --region "$region" >/dev/null
if ! "${gcloud_cmd[@]}" storage buckets describe "gs://${request_bucket}" >/dev/null 2>&1; then
  if ! "${gcloud_cmd[@]}" storage buckets create "gs://${request_bucket}" --location "$region"; then
    "${gcloud_cmd[@]}" storage buckets describe "gs://${request_bucket}" >/dev/null
  fi
fi
service_account="$(
  "${gcloud_cmd[@]}" run services describe "$source_service" \
    --region "$region" \
    --format='value(spec.template.spec.serviceAccountName)'
)"
if [[ -n "$service_account" ]]; then
  "${gcloud_cmd[@]}" storage buckets add-iam-policy-binding "gs://${request_bucket}" \
    --member "serviceAccount:${service_account}" \
    --role roles/storage.objectViewer >/dev/null
fi
python3 - <<PY
import json
print(json.dumps({
    "status": "success",
    "environment": "$environment",
    "job_name": "$job_name",
    "gcp_project": "$gcp_project",
    "source_service": "$source_service",
    "service_account": "$service_account",
    "region": "$region",
    "request_bucket": "$request_bucket",
}, sort_keys=True))
PY
