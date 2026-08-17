import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

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


def _staging_style_manifest() -> dict:
    return {
        "schema_version": 1,
        "providers": {
            "composio": {
                "status": "enabled",
                "display_name": "Composio",
                "sync": {"mode": "full", "include_all_managed_apps": True},
            },
            "pipedream": {
                "status": "enabled",
                "display_name": "Pipedream",
                "sync": {"mode": "partial", "app_slugs": ["SLACK"]},
            },
        },
    }


def test_build_requests_returns_one_payload_per_enabled_provider() -> None:
    module = _request_builder_module()
    manifest = _staging_style_manifest()

    payloads = module.build_requests(
        manifest,
        environment="staging",
        workers=4,
        batch_size=25,
    )

    assert {p["backend_id"] for p in payloads} == {"composio", "pipedream"}
    assert len({p["desired_hash"] for p in payloads}) == 2
    by_backend = {p["backend_id"]: p for p in payloads}
    assert by_backend["composio"]["cache_version"].startswith(
        "public-builtins-staging-composio-",
    )
    assert by_backend["pipedream"]["cache_version"].startswith(
        "public-builtins-staging-pipedream-",
    )
    assert by_backend["composio"]["sync_payload"]["app_slugs"] == []
    assert by_backend["pipedream"]["sync_payload"]["app_slugs"] == ["SLACK"]

    scoped = module.build_requests(
        manifest,
        environment="staging",
        workers=4,
        batch_size=25,
        backend_id="pipedream",
    )
    assert [p["backend_id"] for p in scoped] == ["pipedream"]


def test_build_requests_raises_for_backend_id_with_no_match() -> None:
    module = _request_builder_module()
    manifest = _staging_style_manifest()

    with pytest.raises(ValueError, match="got 0"):
        module.build_requests(
            manifest,
            environment="staging",
            workers=4,
            batch_size=25,
            backend_id="missing",
        )


def test_build_request_still_enforces_exactly_one_provider_match() -> None:
    module = _request_builder_module()
    manifest = _staging_style_manifest()

    with pytest.raises(ValueError, match="got 2"):
        module.build_request(manifest, environment="staging", workers=4, batch_size=25)

    scoped = module.build_request(
        manifest,
        environment="staging",
        workers=4,
        batch_size=25,
        backend_id="composio",
    )
    [expected] = module.build_requests(
        manifest,
        environment="staging",
        workers=4,
        batch_size=25,
        backend_id="composio",
    )
    stable_fields = (
        "backend_id",
        "desired_hash",
        "desired_config",
        "cache_version",
        "sync_payload",
    )
    assert {f: scoped[f] for f in stable_fields} == {
        f: expected[f] for f in stable_fields
    }


def test_cli_stdout_shape_by_provider_count_and_backend_id(tmp_path) -> None:
    single = tmp_path / "single.json"
    single.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "composio": _staging_style_manifest()["providers"]["composio"],
                },
            },
        ),
    )
    multi = tmp_path / "multi.json"
    multi.write_text(json.dumps(_staging_style_manifest()))
    script = ROOT / "deploy/scripts/build_builtins_artifacts_request.py"

    def run(manifest_path, *extra_args):
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--manifest",
                str(manifest_path),
                "--environment",
                "staging",
                *extra_args,
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
        )
        return json.loads(result.stdout)

    single_payload = run(single)
    assert "requests" not in single_payload
    assert single_payload["backend_id"] == "composio"

    scoped_payload = run(multi, "--backend-id", "composio")
    assert "requests" not in scoped_payload
    assert scoped_payload["backend_id"] == "composio"

    multi_payload = run(multi)
    assert set(multi_payload) == {"requests"}
    assert {r["backend_id"] for r in multi_payload["requests"]} == {
        "composio",
        "pipedream",
    }


def test_launcher_dry_run_hash_reporting_for_single_and_multi_provider_manifests(
    tmp_path,
) -> None:
    single = tmp_path / "single.json"
    single.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "composio": _staging_style_manifest()["providers"]["composio"],
                },
            },
        ),
    )
    multi = tmp_path / "multi.json"
    multi.write_text(json.dumps(_staging_style_manifest()))
    script = ROOT / "deploy/scripts/run_seed_builtins_artifacts_job.sh"
    unity_image = (
        "us-central1-docker.pkg.dev/fake-project/unity-repo/unity-staging:deadbeef"
    )

    def dry_run(manifest_path):
        result = subprocess.run(
            [
                "bash",
                str(script),
                "--environment",
                "staging",
                "--manifest",
                str(manifest_path),
                "--unity-image",
                unity_image,
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=True,
        )
        return json.loads(result.stdout)

    single_status = dry_run(single)
    assert "," not in single_status["desired_hash"]
    assert "," not in single_status["run_id"]

    multi_status = dry_run(multi)
    hashes = multi_status["desired_hash"].split(",")
    run_ids = multi_status["run_id"].split(",")
    assert len(hashes) == 2 and len(set(hashes)) == 2
    assert len(run_ids) == 2 and len(set(run_ids)) == 2

    module = _request_builder_module()
    expected = module.build_requests(
        json.loads(multi.read_text()),
        environment="staging",
        workers=4,
        batch_size=25,
    )
    assert set(hashes) == {p["desired_hash"] for p in expected}


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
    assert "UNIFY_INTEGRATION_BOOTSTRAP_EXECUTOR=api" in script
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
