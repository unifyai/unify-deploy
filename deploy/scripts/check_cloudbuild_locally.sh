#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

echo "Checking Cloud Build config references..."
python3 - <<'PY'
from pathlib import Path

checks = {
    "deploy/cloudbuild-staging.yaml": (
        "id: 'start-builtins-artifacts-seed'",
        "--environment staging",
        "--manifest /workspace/integration-manifests/bootstrap.staging.toml",
        "--async",
        "waitFor: ['fetch-unity-integration-manifests', 'push-sha-tag']",
        "unity-staging:${_UNITY_SHA}",
    ),
    "deploy/cloudbuild.yaml": (
        "id: 'start-builtins-artifacts-seed'",
        "--environment production",
        "--manifest /workspace/integration-manifests/bootstrap.production.toml",
        "--async",
        "waitFor: ['fetch-unity-integration-manifests', 'push-sha-tag']",
        "unity:${SHORT_SHA}",
    ),
}

for path, required_strings in checks.items():
    text = Path(path).read_text(encoding="utf-8")
    missing = [required for required in required_strings if required not in text]
    if missing:
        raise SystemExit(f"{path} is missing expected Cloud Build content: {missing}")
    print(f"ok: {path}")
PY

run_seed_dry_run() {
  local environment="$1"
  local manifest="$2"
  local image="$3"

  echo "Dry-running start-builtins-artifacts-seed for ${environment} with TOML bootstrap..."
  local output
  output="$(
    UNITY_FORCE_TOMLI_BOOTSTRAP=true bash deploy/scripts/run_seed_builtins_artifacts_job.sh \
      --environment "$environment" \
      --manifest "$manifest" \
      --unity-image "$image" \
      --unity-project local-unity-project \
      --async \
      --dry-run
  )"
  python3 - "$environment" "$output" <<'PY'
import json
import sys

environment = sys.argv[1]
json_line = next(
    line for line in reversed(sys.argv[2].splitlines()) if line.startswith("{")
)
payload = json.loads(json_line)
expected_job = "unity-seed-builtins-staging" if environment == "staging" else "unity-seed-builtins"
assert payload["status"] == "dry_run", payload
assert payload["environment"] == environment, payload
assert payload["job_name"] == expected_job, payload
assert payload["wait_mode"] == "async", payload
assert payload["desired_hash"], payload
print(json.dumps(payload, sort_keys=True))
PY
}

run_seed_dry_run \
  staging \
  tests/deploy/fixtures/bootstrap.staging.toml \
  us-central1-docker.pkg.dev/local-unity-project/unity/unity-staging:local

run_seed_dry_run \
  production \
  tests/deploy/fixtures/bootstrap.production.toml \
  us-central1-docker.pkg.dev/local-unity-project/unity/unity:local

echo "Local Cloud Build checks passed."
