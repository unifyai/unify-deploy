"""Symbolic tests for built-in integration packages.

Validates that every built-in integration package shipped with the
repository conforms to the manifest schema and passes structural
validation.
"""

import pytest
from pathlib import Path

from unity_deploy.customization.integrations.discovery import (
    _BUILTIN_DIR,
    _load_manifest,
)
from unity_deploy.customization.integrations.types import (
    IntegrationManifest,
    QualityTier,
)
from unity_deploy.customization.integrations.validation import validate_integration

BUILTIN_SLUGS = ["github", "fetch_mcp"]


@pytest.fixture(params=BUILTIN_SLUGS)
def builtin_integration(request) -> tuple[str, Path]:
    slug = request.param
    root = _BUILTIN_DIR / slug
    return slug, root


class TestBuiltinIntegrations:
    def test_manifest_exists(self, builtin_integration):
        slug, root = builtin_integration
        manifest_file = root / "manifest.yaml"
        assert manifest_file.is_file(), f"Missing manifest.yaml for {slug}"

    def test_manifest_parses(self, builtin_integration):
        slug, root = builtin_integration
        manifest = _load_manifest(root / "manifest.yaml")
        assert isinstance(manifest, IntegrationManifest)

    def test_slug_matches_directory(self, builtin_integration):
        slug, root = builtin_integration
        manifest = _load_manifest(root / "manifest.yaml")
        assert manifest.slug == slug

    def test_has_init_py(self, builtin_integration):
        slug, root = builtin_integration
        assert (root / "__init__.py").is_file()

    def test_validation_passes(self, builtin_integration):
        slug, root = builtin_integration
        manifest = _load_manifest(root / "manifest.yaml")
        errors = validate_integration(manifest, root)
        assert errors == [], f"Validation errors for {slug}: {errors}"


class TestGitHubSpecifics:
    def test_tier_is_api(self):
        manifest = _load_manifest(_BUILTIN_DIR / "github" / "manifest.yaml")
        assert manifest.tier == "api"

    def test_quality_is_silver(self):
        manifest = _load_manifest(_BUILTIN_DIR / "github" / "manifest.yaml")
        assert manifest.quality == QualityTier.silver

    def test_no_demo_site(self):
        manifest = _load_manifest(_BUILTIN_DIR / "github" / "manifest.yaml")
        assert manifest.demo_site is None

    def test_has_optional_secret(self):
        manifest = _load_manifest(_BUILTIN_DIR / "github" / "manifest.yaml")
        assert len(manifest.secrets) == 1
        assert manifest.secrets[0].name == "GITHUB_TOKEN"
        assert manifest.secrets[0].required is False

    def test_has_capabilities(self):
        manifest = _load_manifest(_BUILTIN_DIR / "github" / "manifest.yaml")
        assert len(manifest.capabilities) == 3
        ids = {c.id for c in manifest.capabilities}
        assert "user_lookup" in ids
        assert "repo_search" in ids
        assert "issue_triage" in ids

    def test_has_requirements(self):
        manifest = _load_manifest(_BUILTIN_DIR / "github" / "manifest.yaml")
        assert "httpx" in manifest.requirements


class TestFetchMCPSpecifics:
    def test_tier_is_mcp(self):
        manifest = _load_manifest(_BUILTIN_DIR / "fetch_mcp" / "manifest.yaml")
        assert manifest.tier == "mcp"

    def test_quality_is_bronze(self):
        manifest = _load_manifest(_BUILTIN_DIR / "fetch_mcp" / "manifest.yaml")
        assert manifest.quality == QualityTier.bronze

    def test_has_mcp_config(self):
        manifest = _load_manifest(_BUILTIN_DIR / "fetch_mcp" / "manifest.yaml")
        assert manifest.mcp is not None
        assert manifest.mcp.command == "npx"
        assert "@modelcontextprotocol/server-fetch" in manifest.mcp.args

    def test_no_secrets(self):
        manifest = _load_manifest(_BUILTIN_DIR / "fetch_mcp" / "manifest.yaml")
        assert manifest.secrets == []

    def test_no_requirements(self):
        manifest = _load_manifest(_BUILTIN_DIR / "fetch_mcp" / "manifest.yaml")
        assert manifest.requirements == []

    def test_has_capabilities(self):
        manifest = _load_manifest(_BUILTIN_DIR / "fetch_mcp" / "manifest.yaml")
        assert len(manifest.capabilities) == 1
        assert manifest.capabilities[0].id == "web_fetch"

    def test_no_demo_site(self):
        manifest = _load_manifest(_BUILTIN_DIR / "fetch_mcp" / "manifest.yaml")
        assert manifest.demo_site is None
