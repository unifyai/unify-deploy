from pathlib import Path

import yaml

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


def test_the_live_pod_rolebinding_keeps_the_roleref_it_was_created_with() -> None:
    """``roleRef`` is immutable, so this binding can never be re-pointed here.

    The API server rejects any change with ``cannot change roleRef`` — RBAC
    forbids it so a binding cannot be quietly aimed at a more privileged Role.
    ``kubectl apply`` has no way around it, and the deploy step runs under
    ``set -eu``, so editing this value does not merely fail to take effect: it
    fails ``deploy-session-controller``, which gates ``deploy-comms``, and wedges
    the whole pipeline for every environment sharing these manifests.

    Re-scoping the pod SA is therefore a two-step cutover against a *differently
    named* binding — create the new one (RBAC is additive, so no gap), then
    delete this one — never an in-place edit of this roleRef.
    """
    for manifest in CONTROLLER_MANIFESTS:
        for doc in yaml.safe_load_all(manifest.read_text()):
            if not doc or doc.get("kind") != "RoleBinding":
                continue
            if doc["metadata"]["name"] != "assistant-session-runtime-assistant-sa":
                continue
            assert doc["roleRef"]["name"] == "assistant-session-runtime", manifest
