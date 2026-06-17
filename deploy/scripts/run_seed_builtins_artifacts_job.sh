#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Start async seeding for all hosted Builtins artifacts.

Required:
  --environment staging|production
  --manifest PATH | --request-file PATH
  --unity-image IMAGE

Optional:
  --async | --wait
  --workers N
  --batch-size N
  --timeout DURATION
  --backend-id BACKEND
  --dry-run

The core artifact job seeds Builtins functions/guidance from the Unity image.
The integrations artifact job runs the Orchestra materializer from the deployed
Orchestra Cloud Run service image and copies that service's infra configuration.
USAGE
}

environment=""
manifest=""
request_file=""
unity_image=""
workers="4"
batch_size="25"
timeout="3600s"
backend_id=""
wait_mode="async"
dry_run="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment) environment="$2"; shift 2 ;;
    --manifest) manifest="$2"; shift 2 ;;
    --request-file) request_file="$2"; shift 2 ;;
    --unity-image) unity_image="$2"; shift 2 ;;
    --workers) workers="$2"; shift 2 ;;
    --batch-size) batch_size="$2"; shift 2 ;;
    --timeout) timeout="$2"; shift 2 ;;
    --backend-id) backend_id="$2"; shift 2 ;;
    --async) wait_mode="async"; shift ;;
    --wait) wait_mode="wait"; shift ;;
    --dry-run) dry_run="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$environment" || -z "$unity_image" ]]; then
  usage >&2
  exit 2
fi
if [[ -n "$manifest" && -n "$request_file" ]]; then
  echo "Use either --manifest or --request-file, not both." >&2
  exit 2
fi
if [[ -z "$manifest" && -z "$request_file" ]]; then
  echo "One of --manifest or --request-file is required." >&2
  exit 2
fi

case "$environment" in
  staging)
    orchestra_service="orchestra-staging"
    integrations_job="seed-builtins-artifacts-integrations-staging"
    core_job="seed-builtins-artifacts-core-staging"
    orchestra_url="https://internal.example.com/v0"
    ;;
  production)
    orchestra_service="orchestra"
    integrations_job="seed-builtins-artifacts-integrations-production"
    core_job="seed-builtins-artifacts-core-production"
    orchestra_url="https://api.unify.ai/v0"
    ;;
  *) echo "Unsupported environment: ${environment}" >&2; exit 2 ;;
esac

orchestra_project="gcp-project-saas"
orchestra_region="europe-west1"
unity_region="us-central1"
request_bucket="${orchestra_project}-builtins-artifacts-requests"

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

request_json="$tmp_dir/integrations-request.json"
service_json="$tmp_dir/orchestra-service.json"
job_env="$tmp_dir/integrations-job.env"

if [[ -n "$request_file" ]]; then
  cp "$request_file" "$request_json"
else
  request_builder_args=(
    python3 deploy/scripts/build_builtins_artifacts_request.py
    --manifest "$manifest"
    --environment "$environment"
    --workers "$workers"
    --batch-size "$batch_size"
  )
  if [[ -n "$backend_id" ]]; then
    request_builder_args+=(--backend-id "$backend_id")
  fi
  "${request_builder_args[@]}" > "$request_json"
fi

desired_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["desired_hash"])' "$request_json")"
run_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$request_json")"
object_name="builtins-artifacts-seed/${environment}/integrations/${desired_hash}-${run_id}.json"
request_uri="gs://${request_bucket}/${object_name}"
python3 - "$request_json" "$request_uri" <<'PY'
import json
import sys
path, request_uri = sys.argv[1:3]
with open(path, encoding="utf-8") as file:
    payload = json.load(file)
payload["request_uri"] = request_uri
with open(path, "w", encoding="utf-8") as file:
    json.dump(payload, file, sort_keys=True, separators=(",", ":"))
PY

if [[ "$dry_run" == "true" ]]; then
  python3 - <<PY
import json
print(json.dumps({
    "status": "dry_run",
    "executor": "cloud_run_jobs",
    "environment": "$environment",
    "core_job": "$core_job",
    "integrations_job": "$integrations_job",
    "request_uri": "$request_uri",
    "desired_hash": "$desired_hash",
    "run_id": "$run_id",
    "wait_mode": "$wait_mode",
}, sort_keys=True))
PY
  exit 0
fi

bash deploy/scripts/ensure_builtins_artifacts_infra.sh \
  --environment "$environment" \
  --job-name "$integrations_job"

gcloud run services describe "$orchestra_service" \
  --project "$orchestra_project" \
  --region "$orchestra_region" \
  --format=json > "$service_json"
python3 - "$service_json" > "$job_env" <<'PY'
import json
import shlex
import sys

service = json.load(open(sys.argv[1], encoding="utf-8"))
template = ((service.get("spec") or {}).get("template") or {})
spec = template.get("spec") or {}
metadata = template.get("metadata") or {}
annotations = metadata.get("annotations") or {}
containers = spec.get("containers") or []
container = containers[0] if containers else {}
env = container.get("env") or []

def emit(name, value):
    print(f"{name}={shlex.quote(value or '')}")

plain_env = []
secrets = []
for item in env:
    key = item.get("name")
    if not key:
        continue
    if "value" in item:
        plain_env.append(f"{key}={item['value']}")
        continue
    secret_ref = ((item.get("valueFrom") or {}).get("secretKeyRef") or {})
    secret_name = secret_ref.get("name")
    secret_version = secret_ref.get("key") or "latest"
    if secret_name:
        secrets.append(f"{key}={secret_name}:{secret_version}")

emit("ORCHESTRA_IMAGE", container.get("image", ""))
emit("SERVICE_ACCOUNT", spec.get("serviceAccountName", ""))
emit("CLOUD_SQL_INSTANCES", annotations.get("run.googleapis.com/cloudsql-instances", ""))
emit("VPC_CONNECTOR", annotations.get("run.googleapis.com/vpc-access-connector", ""))
emit("ENV_VARS", ",".join(plain_env))
emit("SECRETS", ",".join(secrets))
PY
# shellcheck disable=SC1090
source "$job_env"

if [[ -z "${ORCHESTRA_IMAGE:-}" || -z "${SERVICE_ACCOUNT:-}" ]]; then
  echo "Source Orchestra service did not expose image/service account." >&2
  exit 1
fi
if [[ -z "${CLOUD_SQL_INSTANCES:-}" && "${ENV_VARS:-},${SECRETS:-}" != *"ORCHESTRA_DB"* && "${ENV_VARS:-}" != *"ORCHESTRA_CLOUD_SQL_INSTANCE"* ]]; then
  echo "Source Orchestra service does not expose DB configuration for integrations artifact job." >&2
  exit 1
fi
if [[ "${ENV_VARS:-},${SECRETS:-}" != *"COMPOSIO_API_KEY"* ]]; then
  echo "Source Orchestra service does not expose COMPOSIO_API_KEY for integrations artifact job." >&2
  exit 1
fi

core_flags=(
  "--region=${unity_region}"
  "--image=${unity_image}"
  "--command=python3"
  "--args=scripts/seed_builtins_catalog.py,--skip-integrations"
  "--task-timeout=900s"
  "--max-retries=0"
  "--set-env-vars=ORCHESTRA_URL=${orchestra_url}"
  "--update-secrets=ORCHESTRA_ADMIN_KEY=ORCHESTRA_ADMIN_KEY:latest,UNIFY_KEY=GLOBAL_UNIFY_KEY:latest"
)
if gcloud run jobs describe "$core_job" --region "$unity_region" >/dev/null 2>&1; then
  gcloud run jobs update "$core_job" "${core_flags[@]}"
else
  gcloud run jobs create "$core_job" "${core_flags[@]}"
fi

gcloud --project "$orchestra_project" storage cp "$request_json" "$request_uri"
integration_flags=(
  "--region=${orchestra_region}"
  "--image=${ORCHESTRA_IMAGE}"
  "--command=python3"
  "--args=-m,orchestra.workers.builtins_artifacts_seed_job,--request-gcs-uri,${request_uri}"
  "--task-timeout=${timeout}"
  "--max-retries=0"
  "--service-account=${SERVICE_ACCOUNT}"
)
if [[ -n "${CLOUD_SQL_INSTANCES:-}" ]]; then
  integration_flags+=("--set-cloudsql-instances=${CLOUD_SQL_INSTANCES}")
fi
if [[ -n "${VPC_CONNECTOR:-}" ]]; then
  integration_flags+=("--vpc-connector=${VPC_CONNECTOR}")
fi
if [[ -n "${ENV_VARS:-}" ]]; then
  integration_flags+=("--set-env-vars=${ENV_VARS}")
fi
if [[ -n "${SECRETS:-}" ]]; then
  integration_flags+=("--update-secrets=${SECRETS}")
fi
if gcloud --project "$orchestra_project" run jobs describe "$integrations_job" --region "$orchestra_region" >/dev/null 2>&1; then
  gcloud --project "$orchestra_project" run jobs update "$integrations_job" "${integration_flags[@]}"
else
  gcloud --project "$orchestra_project" run jobs create "$integrations_job" "${integration_flags[@]}"
fi

execute_flags=()
if [[ "$wait_mode" == "async" ]]; then
  execute_flags+=("--async")
else
  execute_flags+=("--wait")
fi
gcloud run jobs execute "$core_job" --region "$unity_region" "${execute_flags[@]}"
gcloud --project "$orchestra_project" run jobs execute "$integrations_job" --region "$orchestra_region" "${execute_flags[@]}"

python3 - <<PY
import json
print(json.dumps({
    "status": "started" if "$wait_mode" == "async" else "success",
    "executor": "cloud_run_jobs",
    "environment": "$environment",
    "core_job": "$core_job",
    "integrations_job": "$integrations_job",
    "request_uri": "$request_uri",
    "desired_hash": "$desired_hash",
    "run_id": "$run_id",
    "wait_mode": "$wait_mode",
}, sort_keys=True))
PY
