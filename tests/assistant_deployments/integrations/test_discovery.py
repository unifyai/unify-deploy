"""Symbolic tests for integration discovery.

Tests multi-path discovery, manifest parsing, and override semantics.
"""

import pytest
from pathlib import Path

import yaml

from unity_deploy.assistant_deployments.integrations.discovery import (
    discover_from_directory,
    discover_integrations,
    _load_manifest,
)
from unity_deploy.assistant_deployments.integrations.types import IntegrationManifest


@pytest.fixture
def integration_dir(tmp_path: Path) -> Path:
    """Create a temporary integration directory with valid packages."""
    pkg1 = tmp_path / "test_pkg"
    pkg1.mkdir()
    (pkg1 / "__init__.py").write_text("")
    (pkg1 / "manifest.yaml").write_text(
        yaml.dump(
            {
                "name": "Test Package",
                "slug": "test_pkg",
                "sector": "test",
                "tier": "api",
                "description": "A test integration package",
            },
        ),
    )

    pkg2 = tmp_path / "another_pkg"
    pkg2.mkdir()
    (pkg2 / "__init__.py").write_text("")
    (pkg2 / "manifest.yaml").write_text(
        yaml.dump(
            {
                "name": "Another Package",
                "slug": "another_pkg",
                "sector": "test",
                "tier": "browser",
                "description": "Another test integration",
            },
        ),
    )

    return tmp_path


@pytest.fixture
def override_dir(tmp_path: Path) -> Path:
    """Create a directory with an override for test_pkg."""
    override = tmp_path / "overrides"
    override.mkdir()

    pkg = override / "test_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "manifest.yaml").write_text(
        yaml.dump(
            {
                "name": "Test Package Override",
                "slug": "test_pkg",
                "sector": "test-override",
                "tier": "mcp",
                "description": "Overridden test package",
                "mcp": {"command": "npx", "args": ["-y", "test-server"]},
            },
        ),
    )

    return override


class TestLoadManifest:
    def test_loads_valid_yaml(self, integration_dir: Path):
        manifest = _load_manifest(integration_dir / "test_pkg" / "manifest.yaml")
        assert isinstance(manifest, IntegrationManifest)
        assert manifest.slug == "test_pkg"
        assert manifest.tier == "api"

    def test_raises_on_invalid_yaml(self, tmp_path: Path):
        bad_file = tmp_path / "manifest.yaml"
        bad_file.write_text("not: valid: yaml: [")
        with pytest.raises(Exception):
            _load_manifest(bad_file)

    def test_raises_on_missing_fields(self, tmp_path: Path):
        bad_file = tmp_path / "manifest.yaml"
        bad_file.write_text(yaml.dump({"name": "Incomplete"}))
        with pytest.raises(Exception):
            _load_manifest(bad_file)


class TestDiscoverFromDirectory:
    def test_discovers_all_packages(self, integration_dir: Path):
        manifests = discover_from_directory(integration_dir)
        assert len(manifests) == 2
        slugs = {m.slug for m in manifests}
        assert slugs == {"test_pkg", "another_pkg"}

    def test_skips_dirs_without_manifest(self, integration_dir: Path):
        no_manifest = integration_dir / "no_manifest_dir"
        no_manifest.mkdir()
        (no_manifest / "__init__.py").write_text("")

        manifests = discover_from_directory(integration_dir)
        assert len(manifests) == 2

    def test_skips_files(self, integration_dir: Path):
        (integration_dir / "random_file.txt").write_text("not a package")
        manifests = discover_from_directory(integration_dir)
        assert len(manifests) == 2

    def test_returns_empty_for_nonexistent_dir(self):
        manifests = discover_from_directory(Path("/nonexistent/path"))
        assert manifests == []

    def test_skips_bad_manifests(self, integration_dir: Path):
        bad_pkg = integration_dir / "bad_pkg"
        bad_pkg.mkdir()
        (bad_pkg / "manifest.yaml").write_text("name: Incomplete")

        manifests = discover_from_directory(integration_dir)
        assert len(manifests) == 2


class TestDiscoverIntegrations:
    def test_discovers_from_extra_paths(self, integration_dir: Path):
        manifests = discover_integrations(extra_paths=[integration_dir])
        slugs = {m.slug for m in manifests}
        assert "test_pkg" in slugs
        assert "another_pkg" in slugs

    def test_extra_paths_override_builtin(
        self,
        integration_dir: Path,
        override_dir: Path,
    ):
        manifests = discover_integrations(
            extra_paths=[integration_dir, override_dir],
        )
        test_pkg = next(m for m in manifests if m.slug == "test_pkg")
        assert test_pkg.sector == "test-override"
        assert test_pkg.tier == "mcp"
