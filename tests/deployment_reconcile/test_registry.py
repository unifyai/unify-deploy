"""Tests for deploy-time client registry loading."""

from __future__ import annotations

import importlib
import sys
import tarfile
from pathlib import Path

import pytest

from unity_deploy.assistant_deployments import clients as clients_mod
from unity_deploy.deployment_reconcile import registry as registry_mod

_CLIENT_MODULE_PREFIX = "unity_deploy.assistant_deployments.clients."


def _clear_embedded_client_state() -> None:
    clients_mod._EMBEDDED_CLIENTS_REGISTERED = False
    clients_mod._CLIENT_DEPLOYMENTS.clear()
    for name in list(sys.modules):
        if name.startswith(_CLIENT_MODULE_PREFIX) and name != (
            "unity_deploy.assistant_deployments.clients"
        ):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _reset_embedded_flag():
    _clear_embedded_client_state()
    yield
    _clear_embedded_client_state()


def test_ensure_embedded_clients_skips_missing_modules(monkeypatch):
    monkeypatch.setenv(
        "ORCHESTRA_URL",
        "https://internal.example.com/v0",
    )
    real_import_module = importlib.import_module

    def selective_import(name, package=None):
        if name.endswith(".unify_company"):
            raise ImportError("unify_company not installed")
        return real_import_module(name, package=package)

    monkeypatch.setattr(importlib, "import_module", selective_import)
    clients_mod._ensure_embedded_clients_registered()
    assert clients_mod._EMBEDDED_CLIENTS_REGISTERED is True
    assert "unify_company" not in clients_mod._CLIENT_DEPLOYMENTS
    assert "client_alpha" in clients_mod._CLIENT_DEPLOYMENTS


def test_load_deployment_registry_embedded_uses_client_imports(monkeypatch):
    monkeypatch.setenv("UNITY_DEPLOY_CLIENT_MODE", "embedded")
    monkeypatch.setenv(
        "ORCHESTRA_URL",
        "https://internal.example.com/v0",
    )
    registered = registry_mod.load_deployment_registry(environment="staging")
    assert "client_alpha" in registered
    assert any(
        t.scope == "assistant" for t in registered["client_alpha"].mapping.targets
    )


def test_load_bundled_registry_builds_entries_from_manifest(monkeypatch):
    monkeypatch.setenv("UNITY_DEPLOY_CLIENT_MODE", "bundled")
    monkeypatch.setenv(
        "ORCHESTRA_URL",
        "https://internal.example.com/v0",
    )

    src = (
        Path(__file__).resolve().parents[2]
        / "unity_deploy"
        / "assistant_deployments"
        / "clients"
        / "client_alpha"
    )

    def fake_download(*, bucket_name, environment, bundle_key, destination):
        assert bundle_key == "client_alpha"
        unpack_root = destination / bundle_key
        unpack_root.mkdir(parents=True)
        archive = destination / f"{bundle_key}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(src, arcname=".")
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(unpack_root, filter="data")
        return unpack_root

    monkeypatch.setattr(registry_mod, "_download_client_bundle", fake_download)

    import unity_deploy.assistant_deployments.routing_manifest as rm

    monkeypatch.setattr(
        rm,
        "load_routing_manifest",
        lambda: {
            "clients": {
                "client_alpha": {
                    "bundle_key": "client_alpha",
                    "environments": {
                        "staging": {
                            "targets": [
                                {
                                    "scope": "assistant",
                                    "scope_id": "2242",
                                    "deployment": "v2",
                                },
                            ],
                        },
                    },
                },
            },
        },
    )

    registered = registry_mod.load_deployment_registry(environment="staging")
    assert set(registered) == {"client_alpha"}
    entry = registered["client_alpha"]
    assert entry.environment == "staging"
    assert entry.mapping.targets[0].scope_id == "2242"
    assert "v2" in entry.specs


def test_download_client_bundle_reads_latest_pointer(monkeypatch, tmp_path):
    blobs: dict[str, bytes] = {}

    class FakeBlob:
        def __init__(self, name: str):
            self.name = name

        def exists(self):
            return self.name in blobs

        def download_as_text(self):
            return blobs[self.name].decode()

        def download_to_filename(self, path: str):
            Path(path).write_bytes(blobs[self.name])

    class FakeBucket:
        def blob(self, name: str):
            return FakeBlob(name)

    class FakeClient:
        def bucket(self, name: str):
            assert name == "unity-client-bundles"
            return FakeBucket()

    src = (
        Path(__file__).resolve().parents[2]
        / "unity_deploy"
        / "assistant_deployments"
        / "clients"
        / "clientzeta"
    )
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src, arcname=".")
    blobs["staging/clientzeta/latest.txt"] = b"abc123"
    blobs["staging/clientzeta/abc123.tar.gz"] = archive.read_bytes()

    monkeypatch.setattr(
        "google.cloud.storage.Client",
        lambda: FakeClient(),
    )
    dest = tmp_path / "out"
    root = registry_mod._download_client_bundle(
        bucket_name="unity-client-bundles",
        environment="staging",
        bundle_key="clientzeta",
        destination=dest,
    )
    assert (root / "deployments").is_dir()
