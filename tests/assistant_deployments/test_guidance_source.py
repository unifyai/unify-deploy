"""Tests for the writable-directory resolution used by regenerated guidance.

``generated_guidance_dir()`` exists because writing under the installed
package's own directory (what deployments did previously) raises
``PermissionError`` in read-only environments such as the
unity-deployment-reconcile Kubernetes Job. These tests pin the contract that
still has to hold: the returned directory lives outside the package tree,
is created on demand, is writable, and isolates namespaces from each other.

The end-to-end "does guidance generation still work" contract for real
client_alpha deployments is covered separately by
``tests/assistant_deployments/clients/test_deployments.py::
TestDeploymentStructure::test_guidance_nonempty``, which drives
``get_deployment()`` for every client_alpha version through this same
code path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unity_deploy.assistant_deployments import guidance_source
from unity_deploy.assistant_deployments.guidance_source import generated_guidance_dir

_PACKAGE_DIR = Path(guidance_source.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _cache_root_under_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        guidance_source,
        "_GENERATED_GUIDANCE_ROOT",
        tmp_path / "unity-deploy-guidance",
    )


class TestGeneratedGuidanceDir:

    def test_resolves_outside_the_installed_package_directory(self) -> None:
        directory = generated_guidance_dir("test_client/v0")
        assert directory != _PACKAGE_DIR
        assert _PACKAGE_DIR not in directory.parents

    def test_directory_is_created_and_writable(self) -> None:
        directory = generated_guidance_dir("test_client/v0")
        assert directory.is_dir()
        probe = directory / "probe.txt"
        probe.write_text("ok", encoding="utf-8")
        assert probe.read_text(encoding="utf-8") == "ok"

    def test_distinct_namespaces_get_isolated_directories(self) -> None:
        first = generated_guidance_dir("client_a/v0")
        second = generated_guidance_dir("client_b/v0")
        assert first != second
        assert first.is_dir() and second.is_dir()

    def test_reused_namespace_returns_the_same_directory(self) -> None:
        first = generated_guidance_dir("client_a/v0")
        second = generated_guidance_dir("client_a/v0")
        assert first == second
