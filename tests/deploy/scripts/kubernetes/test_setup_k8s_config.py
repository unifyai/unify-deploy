import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HELPERS = ROOT / "deploy/scripts/kubernetes/unity_cluster_secrets.py"


def _load_helpers():
    spec = importlib.util.spec_from_file_location("unity_cluster_secrets", HELPERS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_eso_managed_secret_detected_by_annotation() -> None:
    mod = _load_helpers()
    metadata = {
        "annotations": {mod.ESO_MANAGED_ANNOTATION: "abc123"},
    }
    assert mod.is_eso_managed_secret(metadata) is True


def test_non_eso_managed_secret_allows_reconcile() -> None:
    mod = _load_helpers()
    assert mod.is_eso_managed_secret({"annotations": {}}) is False
    assert mod.is_eso_managed_secret(None) is False
