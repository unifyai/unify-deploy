#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Start async seeding for all hosted Builtins artifacts.

Required:
  --environment staging|production
  --manifest PATH
  --droid-image IMAGE

Optional:
  --async | --wait
  --setup-only
  --droid-project PROJECT
  --job-manifest-path PATH_IN_IMAGE
  --workers N
  --batch-size N
  --timeout DURATION
  --backend-id BACKEND
  --dry-run

The job runs from the Droid image and seeds all Builtins artifacts:
functions, guidance, and provider-backed integrations. Orchestra remains the
backend API/materializer for integration context writes; it is not exposed as a
separate Cloud Run Job.
USAGE
}

environment=""
manifest=""
droid_image=""
droid_project=""
job_manifest_path=""
workers="4"
batch_size="25"
timeout="3600s"
backend_id=""
wait_mode="async"
dry_run="false"
setup_only="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment) environment="$2"; shift 2 ;;
    --manifest) manifest="$2"; shift 2 ;;
    --droid-image) droid_image="$2"; shift 2 ;;
    --droid-project) droid_project="$2"; shift 2 ;;
    --job-manifest-path) job_manifest_path="$2"; shift 2 ;;
    --workers) workers="$2"; shift 2 ;;
    --batch-size) batch_size="$2"; shift 2 ;;
    --timeout) timeout="$2"; shift 2 ;;
    --backend-id) backend_id="$2"; shift 2 ;;
    --async) wait_mode="async"; shift ;;
    --wait) wait_mode="wait"; shift ;;
    --setup-only) setup_only="true"; shift ;;
    --dry-run) dry_run="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$environment" || -z "$droid_image" || -z "$manifest" ]]; then
  usage >&2
  exit 2
fi

case "$environment" in
  staging)
    orchestra_service="orchestra-staging"
    job_name="droid-seed-builtins-staging"
    orchestra_url="https://internal.example.com/v0"
    job_manifest_path="${job_manifest_path:-deploy/integrations/bootstrap.staging.toml}"
    ;;
  production)
    orchestra_service="orchestra"
    job_name="droid-seed-builtins"
    orchestra_url="https://api.unify.ai/v0"
    job_manifest_path="${job_manifest_path:-deploy/integrations/bootstrap.production.toml}"
    ;;
  *) echo "Unsupported environment: ${environment}" >&2; exit 2 ;;
esac

droid_region="us-central1"
if [[ -z "$droid_project" ]]; then
  droid_project="$(python3 - "$droid_image" <<'PY'
import re
import sys
match = re.match(r"^[^.]+-docker\.pkg\.dev/([^/]+)/", sys.argv[1])
print(match.group(1) if match else "")
PY
)"
fi
if [[ -z "$droid_project" ]]; then
  echo "Could not derive Droid GCP project from --droid-image; pass --droid-project." >&2
  exit 2
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

ensure_toml_parser() {
  if [[ "${DROID_FORCE_TOMLI_BOOTSTRAP:-false}" != "true" ]] && python3 - <<'PY' >/dev/null 2>&1
try:
    import tomllib
except ModuleNotFoundError:
    import tomli
PY
  then
    return 0
  fi

  tomli_target="${tmp_dir}/python-deps"
  echo "Installing tomli for Python TOML manifest parsing..."
  python3 - "$tomli_target" <<'PY'
from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.request import urlopen
from zipfile import ZipFile

target = Path(sys.argv[1])
target.mkdir(parents=True, exist_ok=True)

with urlopen("https://pypi.org/pypi/tomli/json", timeout=30) as response:
    metadata = json.load(response)

wheel = next(
    file
    for file in metadata["urls"]
    if file["packagetype"] == "bdist_wheel" and file["python_version"] == "py3"
)
wheel_path = target / "tomli.whl"
with urlopen(wheel["url"], timeout=30) as response:
    wheel_path.write_bytes(response.read())

with ZipFile(wheel_path) as archive:
    for member in archive.namelist():
        if member.startswith("tomli/"):
            archive.extract(member, target)
PY
  export PYTHONPATH="${tomli_target}${PYTHONPATH:+:${PYTHONPATH}}"
}

ensure_toml_parser

request_json="$tmp_dir/integrations-request.json"
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

desired_hash="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["desired_hash"])' "$request_json")"
run_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$request_json")"
api_timeout_seconds="${timeout%s}"
if [[ ! "$api_timeout_seconds" =~ ^[0-9]+$ ]]; then
  echo "--timeout must be a duration in whole seconds, for example 3600s." >&2
  exit 2
fi

if [[ "$dry_run" == "true" ]]; then
  python3 - <<PY
import json
print(json.dumps({
    "status": "dry_run",
    "executor": "cloud_run_jobs",
    "environment": "$environment",
    "job_name": "$job_name",
    "droid_project": "$droid_project",
    "job_manifest_path": "$job_manifest_path",
    "desired_hash": "$desired_hash",
    "run_id": "$run_id",
    "wait_mode": "$wait_mode",
    "api_timeout_seconds": "$api_timeout_seconds",
    "setup_only": "$setup_only",
}, sort_keys=True))
PY
  exit 0
fi

droid_project_number="$(gcloud projects describe "$droid_project" --format='value(projectNumber)')"
job_service_account="${droid_project_number}-compute@developer.gserviceaccount.com"
# Secret access for this service account is a one-time environment prerequisite.
# Cloud Build should not mutate Secret Manager IAM on every deploy: the build
# service account may deploy jobs without being allowed to read or update secret
# IAM policies.

job_flags=(
  "--region=${droid_region}"
  "--image=${droid_image}"
  "--command=python3"
  "--args=scripts/seed_builtins_catalog.py,--integration-bootstrap-manifest,${job_manifest_path}"
  "--task-timeout=${timeout}"
  "--max-retries=0"
  "--service-account=${job_service_account}"
  "--set-env-vars=ORCHESTRA_URL=${orchestra_url},DROID_INTEGRATION_BOOTSTRAP_EXECUTOR=api,DROID_INTEGRATION_BOOTSTRAP_TIMEOUT=${api_timeout_seconds}"
  "--update-secrets=ORCHESTRA_ADMIN_KEY=ORCHESTRA_ADMIN_KEY:latest,UNIFY_KEY=GLOBAL_UNIFY_KEY:latest"
)
if gcloud --project "$droid_project" run jobs describe "$job_name" --region "$droid_region" >/dev/null 2>&1; then
  gcloud --project "$droid_project" run jobs update "$job_name" "${job_flags[@]}"
else
  gcloud --project "$droid_project" run jobs create "$job_name" "${job_flags[@]}"
fi

if [[ "$setup_only" == "true" ]]; then
  python3 - <<PY
import json
print(json.dumps({
    "status": "configured",
    "executor": "cloud_run_jobs",
    "environment": "$environment",
    "job_name": "$job_name",
    "droid_project": "$droid_project",
    "job_manifest_path": "$job_manifest_path",
    "desired_hash": "$desired_hash",
    "run_id": "$run_id",
    "wait_mode": "$wait_mode",
    "api_timeout_seconds": "$api_timeout_seconds",
    "setup_only": True,
}, sort_keys=True))
PY
  exit 0
fi

execute_flags=()
if [[ "$wait_mode" == "async" ]]; then
  execute_flags+=("--async")
else
  execute_flags+=("--wait")
fi
gcloud --project "$droid_project" run jobs execute "$job_name" --region "$droid_region" "${execute_flags[@]}"

python3 - <<PY
import json
print(json.dumps({
    "status": "started" if "$wait_mode" == "async" else "success",
    "executor": "cloud_run_jobs",
    "environment": "$environment",
    "job_name": "$job_name",
    "droid_project": "$droid_project",
    "job_manifest_path": "$job_manifest_path",
    "desired_hash": "$desired_hash",
    "run_id": "$run_id",
    "wait_mode": "$wait_mode",
    "api_timeout_seconds": "$api_timeout_seconds",
}, sort_keys=True))
PY
