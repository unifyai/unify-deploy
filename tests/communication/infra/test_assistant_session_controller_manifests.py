from pathlib import Path

# tests/communication/infra/<this file> — the repo root is four parents up. An
# off-by-one does not show up as a wrong answer: every assertion below reads a
# file, so a bad root raises FileNotFoundError and the secret-scoping checks
# these tests are named for never run against a manifest at all.
ROOT = Path(__file__).resolve().parents[3]
CONTROLLER_MANIFESTS = [
    ROOT / "k8s/assistant-session-controller/deployment_staging.yaml",
    ROOT / "k8s/assistant-session-controller/deployment.yaml",
]


def test_controller_manifests_resolve() -> None:
    """Pins the paths, so a bad root cannot quietly disable the checks below."""
    for manifest in CONTROLLER_MANIFESTS:
        assert manifest.is_file(), manifest


def test_assistant_session_controllers_do_not_import_broad_unity_secrets() -> None:
    for manifest in CONTROLLER_MANIFESTS:
        text = manifest.read_text()
        assert "envFrom:" not in text
        assert "GCP_SA_KEY" not in text


def test_assistant_session_controllers_allowlist_required_secret() -> None:
    for manifest in CONTROLLER_MANIFESTS:
        text = manifest.read_text()
        assert "name: ORCHESTRA_ADMIN_KEY" in text
        assert "secretKeyRef:" in text
        assert "name: unity-secrets" in text
        assert "key: ORCHESTRA_ADMIN_KEY" in text
