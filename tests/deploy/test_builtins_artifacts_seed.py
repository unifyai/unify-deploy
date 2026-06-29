import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _request_builder_module():
    path = ROOT / "deploy/scripts/build_builtins_artifacts_request.py"
    spec = importlib.util.spec_from_file_location(
        "build_builtins_artifacts_request",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_request_builder_creates_stable_non_secret_payload() -> None:
    module = _request_builder_module()
    manifest = {
        "schema_version": 1,
        "providers": {
            "composio": {
                "status": "enabled",
                "kind": "composio",
                "display_name": "Composio",
                "sync": {
                    "mode": "partial",
                    "app_slugs": ["gmail"],
                    "prune_unlisted_apps": True,
                },
            },
        },
    }

    first = module.build_request(
        manifest,
        environment="staging",
        workers=4,
        batch_size=25,
    )
    second = module.build_request(
        manifest,
        environment="staging",
        workers=8,
        batch_size=50,
    )

    assert first["desired_hash"] == second["desired_hash"]
    assert first["artifact_kind"] == "integrations"
    assert first["run_id"] != second["run_id"]
    assert first["sync_payload"]["app_slugs"] == ["gmail"]
    assert first["cache_version"].startswith("public-builtins-staging-composio-")
    serialized = str(first)
    assert "COMPOSIO_API_KEY" not in serialized
    assert "SECRET" not in serialized.upper()


def test_request_builder_loads_toml_manifest() -> None:
    module = _request_builder_module()
    manifest = module._load_manifest(
        ROOT / "tests/deploy/fixtures/bootstrap.staging.toml",
    )

    payload = module.build_request(
        manifest,
        environment="staging",
        workers=4,
        batch_size=25,
    )

    assert payload["backend_id"] == "composio"
    assert payload["app_slugs"] == ["GMAIL", "SLACK", "HUBSPOT"]
    assert payload["sync_payload"]["create_auth_configs"] is True
    assert payload["sync_payload"]["include_all_managed_apps"] is False
    assert payload["desired_hash"]


def test_builtins_artifacts_launcher_has_no_inline_api_fallback() -> None:
    script = _read("deploy/scripts/run_seed_builtins_artifacts_job.sh")

    assert "run jobs execute" in script
    assert "import tomllib" in script
    assert "import tomli" in script
    assert "pypi.org/pypi/tomli/json" in script
    assert "ZipFile" in script
    assert "python3 -m pip" not in script
    assert "secrets add-iam-policy-binding" not in script
    assert "--async" in script
    assert "--unity-image" in script
    assert "unity-seed-builtins-staging" in script
    assert "unity-seed-builtins" in script
    assert "--integration-bootstrap-manifest" in script
    assert "UNITY_INTEGRATION_BOOTSTRAP_EXECUTOR=api" in script
    assert "storage cp" not in script
    assert "--request-gcs-uri" not in script
    assert "orchestra.workers.builtins_artifacts_seed_job" not in script
    assert "ORCHESTRA_BUILTINS_SYNC_REQUEST_GCS_URI" not in script
    assert "_BUILTINS_SYNC_ENV_VARS" not in script
    assert "_BUILTINS_SYNC_SECRETS" not in script
    assert "UNIFY_KEY=ORCHESTRA_ADMIN_KEY:latest" in script
    assert "/admin/integrations/builtins-sync/start" not in script
    assert "/v0/logs" not in script


def test_builtins_artifacts_wait_script_exists() -> None:
    waiter = _read("deploy/scripts/wait_builtins_artifacts.sh")

    assert "bootstrap-state" in waiter
    assert "desired_hash" in waiter
    assert "/v0/logs" not in waiter


def test_cloudbuild_staging_starts_builtins_artifacts_seed() -> None:
    config = _read("deploy/cloudbuild-staging.yaml")

    assert "id: 'start-builtins-artifacts-seed'" in config
    assert "run_seed_builtins_artifacts_job.sh" in config
    assert "--environment staging" in config
    assert "--unity-image" in config
    assert "seed-builtins-catalog" not in config
    assert "seed-builtins-integrations" not in config
    assert "ensure_builtins_sync_infra.sh" not in config
    assert "run_builtins_integration_sync_job.sh" not in config
    assert "_BUILTINS_SYNC_JOB_NAME" not in config
    assert "_ORCHESTRA_IMAGE" not in config
    assert "_ORCHESTRA_GCP_PROJECT" not in config
    assert "_ORCHESTRA_SERVICE_NAME" not in config
    assert "_BUILTINS_SYNC_REQUEST_BUCKET" not in config
    assert "--gcp-project" not in config
    assert "--source-service" not in config
    assert '--image "${_ORCHESTRA_IMAGE}"' not in config
    assert "--async" in config
    assert "waitFor: ['fetch-unity-integration-manifests', 'push-sha-tag']" in config
    assert (
        "waitFor: ['seed-builtins-catalog', 'fetch-unity-integration-manifests']"
        not in config
    )
    assert "waitFor: ['seed-builtins-integrations']" not in config
    assert "waitFor: ['push-sha-tag']" in config
    assert "_BUILTINS_SYNC_SERVICE_ACCOUNT" not in config
    assert "_BUILTINS_SYNC_VPC_CONNECTOR" not in config
    assert "_BUILTINS_SYNC_CLOUD_SQL_INSTANCES" not in config
    assert "_BUILTINS_SYNC_ENV_VARS" not in config
    assert "_BUILTINS_SYNC_SECRETS" not in config
    assert "GLOBAL_UNIFY_KEY" not in config
    assert "/admin/integrations/builtins-sync/start" not in config
    assert "/v0/logs" not in config


def test_cloudbuild_production_starts_builtins_artifacts_seed() -> None:
    config = _read("deploy/cloudbuild.yaml")

    assert "id: 'start-builtins-artifacts-seed'" in config
    assert "run_seed_builtins_artifacts_job.sh" in config
    assert "--environment production" in config
    assert "--unity-image" in config
    assert "seed-builtins-catalog" not in config
    assert "seed-builtins-integrations" not in config
    assert "ensure_builtins_sync_infra.sh" not in config
    assert "run_builtins_integration_sync_job.sh" not in config
    assert "_BUILTINS_SYNC_JOB_NAME" not in config
    assert "_ORCHESTRA_IMAGE" not in config
    assert "_ORCHESTRA_GCP_PROJECT" not in config
    assert "_ORCHESTRA_SERVICE_NAME" not in config
    assert "_BUILTINS_SYNC_REQUEST_BUCKET" not in config
    assert "--gcp-project" not in config
    assert "--source-service" not in config
    assert '--image "${_ORCHESTRA_IMAGE}"' not in config
    assert "--async" in config
    assert "waitFor: ['fetch-unity-integration-manifests', 'push-sha-tag']" in config
    assert (
        "waitFor: ['seed-builtins-catalog', 'fetch-unity-integration-manifests']"
        not in config
    )
    assert "waitFor: ['seed-builtins-integrations']" not in config
    assert "waitFor: ['push-sha-tag']" in config
    assert "_BUILTINS_SYNC_SERVICE_ACCOUNT" not in config
    assert "_BUILTINS_SYNC_VPC_CONNECTOR" not in config
    assert "_BUILTINS_SYNC_CLOUD_SQL_INSTANCES" not in config
    assert "_BUILTINS_SYNC_ENV_VARS" not in config
    assert "_BUILTINS_SYNC_SECRETS" not in config
    assert "GLOBAL_UNIFY_KEY" not in config
    assert "/admin/integrations/builtins-sync/start" not in config
    assert "/v0/logs" not in config
