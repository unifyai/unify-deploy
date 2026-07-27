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


def test_meet_twin_credentials_are_environment_scoped() -> None:
    """Each environment signs Meet in as its own twin account.

    Staging and production share the secret *names* inside the pod but must
    resolve to different Secret Manager entries — a production pod warming the
    staging twin's cookies would sign the assistant in as the wrong identity.
    """
    for filename, suffix in (
        ("unity-secrets-external-secret_staging.yaml", "staging"),
        ("unity-secrets-external-secret_production.yaml", "production"),
    ):
        entries = _load_yaml(filename)["spec"]["data"]
        data = {item["secretKey"]: item for item in entries}
        for secret_key, remote_stem in (
            ("MEET_TWIN_EMAIL", "meet-twin-email"),
            ("MEET_TWIN_PASSWORD", "meet-twin-password"),
        ):
            assert secret_key in data, f"{secret_key} missing from {filename}"
            assert data[secret_key]["remoteRef"]["key"] == f"{remote_stem}-{suffix}"
