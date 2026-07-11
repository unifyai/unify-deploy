"""Tests for bundled client deployment loading."""

from __future__ import annotations

import importlib
import sys
import tarfile
from pathlib import Path

import pytest

from unity_deploy.client_bundle.loader import (
    _register_bundle_package,
    resolve_from_bundle,
)


@pytest.fixture
def client_alpha_bundle(tmp_path, monkeypatch):
    client_root = tmp_path / "client_alpha"
    src = (
        Path(__file__).resolve().parents[2]
        / "unity_deploy"
        / "assistant_deployments"
        / "clients"
        / "client_alpha"
    )
    archive = tmp_path / "client_alpha.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(src, arcname=".")
    extract_root = client_root
    extract_root.mkdir()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(extract_root)
    monkeypatch.setenv("CLIENT_DEPLOYMENT_ROOT", str(extract_root))
    return extract_root


def test_resolve_from_bundle_client_alpha_v2(client_alpha_bundle):
    resolved = resolve_from_bundle(
        client_name="client_alpha",
        deployment="v2",
        assistant_id=2242,
    )
    assert resolved is not None
    assert resolved.guidance_dirs
    assert any("client_alpha" in str(path) for path in resolved.guidance_dirs)


def test_register_bundle_package_executes_real_init(tmp_path):
    """Registration must not stub packages — real ``__init__.py`` exports load."""

    client_name = "marker_client"
    root = tmp_path / client_name
    impl = root / "deployments" / "default" / "functions" / "_impl"
    impl.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "deployments" / "__init__.py").write_text("", encoding="utf-8")
    (root / "deployments" / "default" / "__init__.py").write_text("", encoding="utf-8")
    (root / "deployments" / "default" / "functions" / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    (impl / "__init__.py").write_text("MARKER = 'from-real-init'\n", encoding="utf-8")

    client_pkg = f"unity_deploy.assistant_deployments.clients.{client_name}"
    for name in list(sys.modules):
        if name == client_pkg or name.startswith(f"{client_pkg}."):
            del sys.modules[name]

    _register_bundle_package(client_name, root)
    loaded = importlib.import_module(
        f"{client_pkg}.deployments.default.functions._impl",
    )
    assert loaded.MARKER == "from-real-init"
