from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER_MANIFESTS = [
    ROOT / "k8s/assistant-session-controller/deployment_staging.yaml",
    ROOT / "k8s/assistant-session-controller/deployment.yaml",
]


def test_assistant_session_controllers_do_not_import_broad_droid_secrets() -> None:
    for manifest in CONTROLLER_MANIFESTS:
        text = manifest.read_text()
        assert "envFrom:" not in text
        assert "GCP_SA_KEY" not in text


def test_assistant_session_controllers_allowlist_required_secret() -> None:
    for manifest in CONTROLLER_MANIFESTS:
        text = manifest.read_text()
        assert "name: ORCHESTRA_ADMIN_KEY" in text
        assert "secretKeyRef:" in text
        assert "name: droid-secrets" in text
        assert "key: ORCHESTRA_ADMIN_KEY" in text
