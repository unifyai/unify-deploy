from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
SECRETS_DIR = ROOT / "deploy/k8s/secrets"


def _load_yaml(name: str) -> dict:
    return yaml.safe_load((SECRETS_DIR / name).read_text())


def test_cluster_secret_store_targets_gcp_project() -> None:
    doc = _load_yaml("cluster-secret-store.yaml")
    assert doc["spec"]["provider"]["gcpsm"]["projectID"] == "gcp-project-runtime"
    ref = doc["spec"]["provider"]["gcpsm"]["auth"]["secretRef"][
        "secretAccessKeySecretRef"
    ]
    assert ref["name"] == "external-secrets-gcp-credentials"
    assert ref["namespace"] == "external-secrets"


def test_external_secret_targets_match_namespace() -> None:
    for filename, namespace in (
        ("unity-secrets-external-secret_staging.yaml", "staging"),
        ("unity-secrets-external-secret_production.yaml", "production"),
    ):
        doc = _load_yaml(filename)
        assert doc["metadata"]["namespace"] == namespace
        assert doc["spec"]["target"]["name"] == "unity-secrets"
        assert doc["spec"]["secretStoreRef"]["name"] == "unity-gcp-secret-manager"


def test_openrouter_key_maps_from_secret_manager() -> None:
    doc = _load_yaml("unity-secrets-external-secret_staging.yaml")
    entries = [
        item
        for item in doc["spec"]["data"]
        if item["secretKey"] == "OPENROUTER_API_KEY"
    ]
    assert len(entries) == 1
    assert entries[0]["remoteRef"]["key"] == "OPENROUTER_API_KEY"


def test_tavily_key_maps_from_secret_manager() -> None:
    doc = _load_yaml("unity-secrets-external-secret_staging.yaml")
    tavily_entries = [
        item for item in doc["spec"]["data"] if item["secretKey"] == "TAVILY_API_KEY"
    ]
    assert len(tavily_entries) == 1
    assert tavily_entries[0]["remoteRef"]["key"] == "TAVILY_API_KEY"


def test_recall_api_key_is_environment_scoped() -> None:
    """Staging and production must hold separate Recall workspaces/keys.

    Bot hours bill per key, so a shared one makes staging spend
    indistinguishable from production -- and a staging key that could read
    production recordings is a data-boundary problem, not just an accounting
    one. Note the UPPER_SNAKE_ENV remote naming: this follows the Cloud Run
    secrets (SLACK_SIGNING_SECRET_PROD), not the lower-kebab twin keys above.
    """
    for filename, suffix in (
        ("unity-secrets-external-secret_staging.yaml", "STAGING"),
        ("unity-secrets-external-secret_production.yaml", "PROD"),
    ):
        data = {
            item["secretKey"]: item["remoteRef"]["key"]
            for item in _load_yaml(filename)["spec"]["data"]
        }
        for secret_key in ("RECALL_API_KEY", "RECALL_RELAY_SECRET"):
            assert secret_key in data, f"{secret_key} missing from {filename}"
            assert data[secret_key] == f"{secret_key}_{suffix}"
