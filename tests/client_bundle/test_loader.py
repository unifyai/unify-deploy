"""Tests for bundled client deployment loading."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from unity_deploy.client_bundle.loader import resolve_from_bundle


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
    assert any(path.name == "guidance" for path in resolved.guidance_dirs)
