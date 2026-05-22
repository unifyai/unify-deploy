from pathlib import Path
from types import SimpleNamespace

from communication.infra.helpers import create_unity_job

ROOT = Path(__file__).resolve().parents[2]


def test_create_unity_job_uses_explicit_env_allowlist() -> None:
    text = (ROOT / "communication/infra/helpers.py").read_text()

    create_job_source = text[text.index("def create_unity_job(") :]
    create_job_source = create_job_source[
        : create_job_source.index("def get_job_logs(")
    ]
    config_env_source = create_job_source[
        create_job_source.index("unity_config_env = []") : create_job_source.index(
            "unity_secret_env = [",
        )
    ]

    assert '"envFrom"' not in create_job_source
    assert "GCP_SA_KEY" not in create_job_source
    assert '"ORCHESTRA_ADMIN_KEY"' in create_job_source
    assert '"GCP_PROJECT_ID"' in create_job_source
    assert '"UNITY_STARTUP_TIMING"' in create_job_source
    assert '"UNITY_STARTUP_TIMING"' not in config_env_source
    assert '"value": "1" if deploy_env == "staging" else "0"' in create_job_source
    assert '"UNITY_DEPLOY_RUNTIME_RECONCILE_MODE"' in create_job_source
    assert (
        'optional_unity_config_keys = {"UNITY_DEPLOY_RUNTIME_RECONCILE_MODE"}'
        in create_job_source
    )
    assert 'config_ref["optional"] = True' in create_job_source


def test_create_unity_job_applies_explicit_service_url_env_once() -> None:
    class FakeBatchApi:
        def __init__(self):
            self.created_body = None

        def create_namespaced_job(self, namespace, body):
            self.created_body = body
            return SimpleNamespace(
                metadata=SimpleNamespace(name=body["metadata"]["name"], uid="uid-1"),
            )

    batch_api = FakeBatchApi()

    create_unity_job(
        batch_api,
        job_name="unity-preview-1207-staging",
        namespace="staging",
        deploy_env="staging",
        extra_env={
            "ORCHESTRA_URL": "https://internal.example.com/v0",
            "UNITY_COMMS_URL": "https://myslug---unity-comms-app-staging.run.app",
            "UNITY_ADAPTERS_URL": ("https://myslug---unity-adapters-staging.run.app"),
        },
    )

    env_vars = batch_api.created_body["spec"]["template"]["spec"]["containers"][0][
        "env"
    ]
    env_by_name = {env_var["name"]: env_var for env_var in env_vars}
    env_names = [env_var["name"] for env_var in env_vars]

    assert env_names.count("ORCHESTRA_URL") == 1
    assert env_names.count("UNITY_COMMS_URL") == 1
    assert env_names.count("UNITY_ADAPTERS_URL") == 1
    assert env_by_name["ORCHESTRA_URL"]["value"] == (
        "https://internal.example.com/v0"
    )
    assert env_by_name["UNITY_COMMS_URL"]["value"] == (
        "https://myslug---unity-comms-app-staging.run.app"
    )
    assert env_by_name["UNITY_ADAPTERS_URL"]["value"] == (
        "https://myslug---unity-adapters-staging.run.app"
    )


def test_create_unity_job_always_pulls_latest_image() -> None:
    class FakeBatchApi:
        def __init__(self):
            self.created_body = None

        def create_namespaced_job(self, namespace, body):
            self.created_body = body
            return SimpleNamespace(
                metadata=SimpleNamespace(name=body["metadata"]["name"], uid="uid-1"),
            )

    batch_api = FakeBatchApi()

    create_unity_job(
        batch_api,
        job_name="unity-offline-latest-staging",
        namespace="staging",
        image="registry/unity-staging:latest",
    )

    container = batch_api.created_body["spec"]["template"]["spec"]["containers"][0]
    assert container["imagePullPolicy"] == "Always"


def test_create_unity_job_uses_cache_for_immutable_image_tags() -> None:
    class FakeBatchApi:
        def __init__(self):
            self.created_body = None

        def create_namespaced_job(self, namespace, body):
            self.created_body = body
            return SimpleNamespace(
                metadata=SimpleNamespace(name=body["metadata"]["name"], uid="uid-1"),
            )

    batch_api = FakeBatchApi()

    create_unity_job(
        batch_api,
        job_name="unity-offline-sha-staging",
        namespace="staging",
        image="registry/unity-staging:4f25e7cbd9a4",
    )

    container = batch_api.created_body["spec"]["template"]["spec"]["containers"][0]
    assert container["imagePullPolicy"] == "IfNotPresent"


def test_comms_preview_deploy_sets_all_runtime_service_urls() -> None:
    text = (ROOT / "cloudbuild/unity-comms-app-preview.yaml").read_text()

    assert 'ORCHESTRA_URL="https://$${SLUG}.${_PEER_ORCHESTRA_HOST}/v0"' in text
    assert 'COMMS_URL="https://$${SLUG}---${_PEER_COMMS_HOST}"' in text
    assert 'ADAPTERS_URL="https://$${SLUG}---${_PEER_ADAPTERS_HOST}"' in text
    assert "UNITY_COMMS_URL=$${COMMS_URL}" in text
    assert "UNITY_ADAPTERS_URL=$${ADAPTERS_URL}" in text


def test_comms_preview_restore_resets_all_runtime_service_urls() -> None:
    text = (ROOT / "cloudbuild/unity-comms-app-preview.yaml").read_text()

    canonical_runtime_env = (
        "--update-env-vars=DEPLOY_ENV=staging,"
        "ORCHESTRA_URL=${_CANONICAL_ORCHESTRA_URL},"
        "UNITY_COMMS_URL=https://${_PEER_COMMS_HOST},"
        "UNITY_ADAPTERS_URL=https://${_PEER_ADAPTERS_HOST}"
    )

    assert text.count(canonical_runtime_env) == 3


def test_comms_staging_deploy_resets_runtime_service_urls() -> None:
    text = (ROOT / "cloudbuild/unity-comms-app-staging.yaml").read_text()

    canonical_runtime_env = (
        "--update-env-vars=DEPLOY_ENV=staging,"
        "ORCHESTRA_URL=${_ORCHESTRA_URL},"
        "UNITY_COMMS_URL=https://${_COMMS_HOST},"
        "UNITY_ADAPTERS_URL=https://${_ADAPTERS_HOST}"
    )

    assert canonical_runtime_env in text
    assert "_ORCHESTRA_URL: 'https://internal.example.com/v0'" in text
    assert "_COMMS_HOST: 'service.a.run.app'" in text
    assert "_ADAPTERS_HOST: 'service.a.run.app'" in text
