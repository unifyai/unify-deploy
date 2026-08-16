#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Start async seeding for all hosted Builtins artifacts.

Required:
  --environment staging|production
  --manifest PATH
  --unity-image IMAGE

Optional:
  --async | --wait
  --setup-only
  --unity-project PROJECT
  --job-manifest-path PATH_IN_IMAGE
  --workers N
  --batch-size N
  --timeout DURATION
  --backend-id BACKEND
  --force-override manifest|true|false
  --dry-run

A full resync can be requested declaratively via ``sync.force = true`` in the
manifest, or operationally for this job revision with
``--force-override true`` / ``UNIFY_INTEGRATION_BOOTSTRAP_FORCE_OVERRIDE=true``.
Use ``false`` to suppress a manifest force value without changing code.

The job runs from the Unity image and seeds all Builtins artifacts:
functions, guidance, and provider-backed integrations. Orchestra remains the
backend API/materializer for integration context writes; it is not exposed as a
separate Cloud Run Job.
USAGE
}

environment=""
manifest=""
unity_image=""
unity_project=""
job_manifest_path=""
workers="4"
batch_size="25"
timeout="7200s"
backend_id=""
wait_mode="async"
dry_run="false"
setup_only="false"
force_override="${UNIFY_INTEGRATION_BOOTSTRAP_FORCE_OVERRIDE:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment) environment="$2"; shift 2 ;;
    --manifest) manifest="$2"; shift 2 ;;
    --unity-image) unity_image="$2"; shift 2 ;;
    --unity-project) unity_project="$2"; shift 2 ;;
    --job-manifest-path) job_manifest_path="$2"; shift 2 ;;
    --workers) workers="$2"; shift 2 ;;
    --batch-size) batch_size="$2"; shift 2 ;;
    --timeout) timeout="$2"; shift 2 ;;
    --backend-id) backend_id="$2"; shift 2 ;;
    --force-override) force_override="$2"; shift 2 ;;
    --async) wait_mode="async"; shift ;;
    --wait) wait_mode="wait"; shift ;;
    --setup-only) setup_only="true"; shift ;;
    --dry-run) dry_run="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$environment" || -z "$unity_image" || -z "$manifest" ]]; then
  usage >&2
  exit 2
fi

case "${force_override,,}" in
  ""|manifest|auto|default|unset|1|true|yes|on|force|forced|0|false|no|off|skip|disabled|disable) ;;
  *)
    echo "--force-override must be one of manifest/auto, true/on, or false/off." >&2
    exit 2
    ;;
esac

case "$environment" in
  staging)
    orchestra_service="orchestra-staging"
    job_name="unity-seed-builtins-staging"
    orchestra_url="https://internal.example.com/v0"
    job_manifest_path="${job_manifest_path:-deploy/integrations/bootstrap.staging.toml}"
    ;;
  production)
    orchestra_service="orchestra"
    job_name="unity-seed-builtins"
    orchestra_url="https://api.unify.ai/v0"
    job_manifest_path="${job_manifest_path:-deploy/integrations/bootstrap.production.toml}"
    ;;
  *) echo "Unsupported environment: ${environment}" >&2; exit 2 ;;
esac

unity_region="us-central1"
if [[ -z "$unity_project" ]]; then
  unity_project="$(python3 - "$unity_image" <<'PY'
import re
import sys
match = re.match(r"^[^.]+-docker\.pkg\.dev/([^/]+)/", sys.argv[1])
print(match.group(1) if match else "")
PY
)"
fi
if [[ -z "$unity_project" ]]; then
  echo "Could not derive Unity GCP project from --unity-image; pass --unity-project." >&2
  exit 2
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

ensure_toml_parser() {
  if [[ "${UNIFY_FORCE_TOMLI_BOOTSTRAP:-false}" != "true" ]] && python3 - <<'PY' >/dev/null 2>&1
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

# The request builder emits a single payload when the manifest resolves to
# one enabled provider, or ``{"requests": [...]}`` when it resolves to
# several (the seed job always seeds every enabled provider from the
# manifest in one run, so status reporting joins per-provider values).
desired_hash="$(python3 -c '
import json, sys
data = json.load(open(sys.argv[1]))
requests = data.get("requests")
print(",".join(r["desired_hash"] for r in requests) if requests else data["desired_hash"])
' "$request_json")"
run_id="$(python3 -c '
import json, sys
data = json.load(open(sys.argv[1]))
requests = data.get("requests")
print(",".join(r["run_id"] for r in requests) if requests else data["run_id"])
' "$request_json")"
# ``timeout`` bounds the Cloud Run task (the trigger + the poll loop that waits
# for the Orchestra-launched worker job). The seed endpoint returns 202 quickly,
# so the POST itself only needs a short trigger timeout; the bulk of the budget
# is the poll, which must finish before Cloud Run kills the task.
task_timeout_seconds="${timeout%s}"
if [[ ! "$task_timeout_seconds" =~ ^[0-9]+$ ]]; then
  echo "--timeout must be a duration in whole seconds, for example 7200s." >&2
  exit 2
fi
trigger_timeout_seconds="${UNIFY_INTEGRATION_BOOTSTRAP_TRIGGER_TIMEOUT:-300}"
poll_timeout_seconds=$(( task_timeout_seconds - 300 ))
if (( poll_timeout_seconds < 600 )); then
  poll_timeout_seconds=600
fi

job_env_vars="ORCHESTRA_URL=${orchestra_url},UNIFY_INTEGRATION_BOOTSTRAP_EXECUTOR=api,UNIFY_INTEGRATION_BOOTSTRAP_TIMEOUT=${trigger_timeout_seconds},UNIFY_INTEGRATION_BOOTSTRAP_POLL_TIMEOUT=${poll_timeout_seconds}"
if [[ -n "$force_override" ]]; then
  job_env_vars="${job_env_vars},UNIFY_INTEGRATION_BOOTSTRAP_FORCE_OVERRIDE=${force_override}"
fi

if [[ "$dry_run" == "true" ]]; then
  python3 - <<PY
import json
print(json.dumps({
    "status": "dry_run",
    "executor": "cloud_run_jobs",
    "environment": "$environment",
    "job_name": "$job_name",
    "unity_project": "$unity_project",
    "job_manifest_path": "$job_manifest_path",
    "desired_hash": "$desired_hash",
    "run_id": "$run_id",
    "wait_mode": "$wait_mode",
    "task_timeout_seconds": "$task_timeout_seconds",
    "trigger_timeout_seconds": "$trigger_timeout_seconds",
    "poll_timeout_seconds": "$poll_timeout_seconds",
    "setup_only": "$setup_only",
    "force_override": "$force_override",
}, sort_keys=True))
PY
  exit 0
fi

unity_project_number="$(gcloud projects describe "$unity_project" --format='value(projectNumber)')"
job_service_account="${unity_project_number}-compute@developer.gserviceaccount.com"
# Secret access for this service account is a one-time environment prerequisite.
# Cloud Build should not mutate Secret Manager IAM on every deploy: the build
# service account may deploy jobs without being allowed to read or update secret
# IAM policies.

job_flags=(
  "--region=${unity_region}"
  "--image=${unity_image}"
  "--command=python3"
  "--args=scripts/seed_builtins_catalog.py,--integration-bootstrap-manifest,${job_manifest_path}"
  "--task-timeout=${timeout}"
  "--max-retries=0"
  "--service-account=${job_service_account}"
  "--set-env-vars=${job_env_vars}"
  "--update-secrets=ORCHESTRA_ADMIN_KEY=ORCHESTRA_ADMIN_KEY:latest,UNIFY_KEY=ORCHESTRA_ADMIN_KEY:latest"
)
if gcloud --project "$unity_project" run jobs describe "$job_name" --region "$unity_region" >/dev/null 2>&1; then
  gcloud --project "$unity_project" run jobs update "$job_name" "${job_flags[@]}"
else
  gcloud --project "$unity_project" run jobs create "$job_name" "${job_flags[@]}"
fi

if [[ "$setup_only" == "true" ]]; then
  python3 - <<PY
import json
print(json.dumps({
    "status": "configured",
    "executor": "cloud_run_jobs",
    "environment": "$environment",
    "job_name": "$job_name",
    "unity_project": "$unity_project",
    "job_manifest_path": "$job_manifest_path",
    "desired_hash": "$desired_hash",
    "run_id": "$run_id",
    "wait_mode": "$wait_mode",
    "task_timeout_seconds": "$task_timeout_seconds",
    "trigger_timeout_seconds": "$trigger_timeout_seconds",
    "poll_timeout_seconds": "$poll_timeout_seconds",
    "force_override": "$force_override",
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
gcloud --project "$unity_project" run jobs execute "$job_name" --region "$unity_region" "${execute_flags[@]}"

python3 - <<PY
import json
print(json.dumps({
    "status": "started" if "$wait_mode" == "async" else "success",
    "executor": "cloud_run_jobs",
    "environment": "$environment",
    "job_name": "$job_name",
    "unity_project": "$unity_project",
    "job_manifest_path": "$job_manifest_path",
    "desired_hash": "$desired_hash",
    "run_id": "$run_id",
    "wait_mode": "$wait_mode",
    "task_timeout_seconds": "$task_timeout_seconds",
    "trigger_timeout_seconds": "$trigger_timeout_seconds",
    "poll_timeout_seconds": "$poll_timeout_seconds",
    "force_override": "$force_override",
}, sort_keys=True))
PY
