"""Symbolic tests for built-in integration packages across all roots.

Validates that every integration package shipped with the repository --
generic, client, and mock -- conforms to the manifest schema and passes
structural validation. Each root is asserted separately so future packages
can be added in the right location without touching tests in the other roots.
"""

import ast
import pytest
from pathlib import Path

from unity_deploy.assistant_deployments.integrations.discovery import (
    _BUILTIN_DIR,
    _CLIENT_DIR,
    _MOCK_DIR,
    discover_from_directory,
    _load_manifest,
)
from unity_deploy.assistant_deployments.integrations.types import (
    IntegrationManifest,
    QualityTier,
)
from unity_deploy.assistant_deployments.integrations.validation import (
    validate_integration,
)

GENERIC_SLUGS = ["github", "fetch_mcp"]
CLIENT_SLUGS = ["clientepsilon_homes_compliance", "client_alpha_repairs"]
MOCK_SLUGS = ["clientepsilon_homes_compliance_mock", "client_alpha_repairs_mock"]
INTEGRATIONS_ROOT = _BUILTIN_DIR.parent
DEPLOYMENTS_ROOT = INTEGRATIONS_ROOT.parent


def _available_package_slugs() -> set[str]:
    slugs: set[str] = set()
    for root in [_BUILTIN_DIR, _CLIENT_DIR, _MOCK_DIR]:
        slugs.update(manifest.slug for manifest in discover_from_directory(root))
    return slugs


def _declared_deployment_integration_slugs() -> set[str]:
    slugs: set[str] = set()
    for py_file in DEPLOYMENTS_ROOT.rglob("*.py"):
        if INTEGRATIONS_ROOT in py_file.parents:
            continue
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "integrations":
                    continue
                try:
                    value = ast.literal_eval(keyword.value)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, str):
                    slugs.add(value)
                else:
                    slugs.update(str(item) for item in value)
    return slugs


@pytest.fixture(params=GENERIC_SLUGS)
def builtin_integration(request) -> tuple[str, Path]:
    slug = request.param
    root = _BUILTIN_DIR / slug
    return slug, root


@pytest.fixture(params=CLIENT_SLUGS)
def client_integration(request) -> tuple[str, Path]:
    slug = request.param
    root = _CLIENT_DIR / slug
    return slug, root


@pytest.fixture(params=MOCK_SLUGS)
def mock_integration(request) -> tuple[str, Path]:
    slug = request.param
    root = _MOCK_DIR / slug
    return slug, root


class TestBuiltinIntegrations:
    """Generic packages: reusable platform/provider connectors."""

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


class TestProviderBackedBoundary:
    """Guardrails that keep dynamic provider apps out of Level 3 packages."""

    def test_boundary_doc_is_linked_from_readme(self):
        readme = (INTEGRATIONS_ROOT / "README.md").read_text()
        normalized_readme = " ".join(readme.split())
        assert "PROVIDER_BACKED_INTEGRATIONS.md" in readme
        assert "DeploymentSpec.integrations" in normalized_readme
        assert "disk package slugs" in normalized_readme

    def test_every_discovered_level_three_manifest_validates(self):
        roots = [_BUILTIN_DIR, _CLIENT_DIR, _MOCK_DIR]
        for root in roots:
            for manifest in discover_from_directory(root):
                package_root = root / manifest.slug
                errors = validate_integration(manifest, package_root)
                assert errors == [], f"Validation errors for {manifest.slug}: {errors}"

    def test_deployment_integration_slugs_resolve_to_package_roots(self):
        available = _available_package_slugs()
        declared = _declared_deployment_integration_slugs()

        missing = sorted(declared - available)
        assert missing == [], (
            "DeploymentSpec.integrations and SeedLayer(integrations=[...]) "
            f"must reference discovered Level 3 package slugs, got missing: {missing}"
        )

    def test_provider_backends_are_not_placeholder_packages(self):
        for backend_slug in ["composio", "pipedream"]:
            assert backend_slug not in _available_package_slugs(), (
                f"{backend_slug} is a provider backend owned by Orchestra; "
                "do not add a placeholder unity-deploy package for it."
            )

    def test_common_provider_apps_are_not_placeholder_packages(self):
        provider_app_slugs = {
            "airtable",
            "gmail",
            "google_drive",
            "linear",
            "notion",
            "slack",
        }

        assert provider_app_slugs.isdisjoint(_available_package_slugs()), (
            "Dynamic provider app catalogs belong in Orchestra. Add a Level 3 "
            "package only when the app needs custom unity-deploy runtime code."
        )


class TestClientIntegrations:
    """Client packages: private real connectors or compositions."""

    def test_manifest_exists(self, client_integration):
        slug, root = client_integration
        manifest_file = root / "manifest.yaml"
        assert manifest_file.is_file(), f"Missing manifest.yaml for {slug}"

    def test_manifest_parses(self, client_integration):
        slug, root = client_integration
        manifest = _load_manifest(root / "manifest.yaml")
        assert isinstance(manifest, IntegrationManifest)

    def test_slug_matches_directory(self, client_integration):
        slug, root = client_integration
        manifest = _load_manifest(root / "manifest.yaml")
        assert manifest.slug == slug

    def test_has_init_py(self, client_integration):
        slug, root = client_integration
        assert (root / "__init__.py").is_file()

    def test_validation_passes(self, client_integration):
        slug, root = client_integration
        manifest = _load_manifest(root / "manifest.yaml")
        errors = validate_integration(manifest, root)
        assert errors == [], f"Validation errors for {slug}: {errors}"


class TestMockIntegrations:
    """Mock packages: opt-in deterministic test doubles for demos and E2E."""

    def test_manifest_exists(self, mock_integration):
        slug, root = mock_integration
        manifest_file = root / "manifest.yaml"
        assert manifest_file.is_file(), f"Missing manifest.yaml for {slug}"

    def test_manifest_parses(self, mock_integration):
        slug, root = mock_integration
        manifest = _load_manifest(root / "manifest.yaml")
        assert isinstance(manifest, IntegrationManifest)

    def test_slug_matches_directory(self, mock_integration):
        slug, root = mock_integration
        manifest = _load_manifest(root / "manifest.yaml")
        assert manifest.slug == slug

    def test_slug_has_mock_suffix(self, mock_integration):
        slug, _root = mock_integration
        assert slug.endswith("_mock"), (
            f"Mock package '{slug}' must end with '_mock' so opt-in "
            f"activation can identify it without explicit flags."
        )

    def test_has_init_py(self, mock_integration):
        slug, root = mock_integration
        assert (root / "__init__.py").is_file()

    def test_validation_passes(self, mock_integration):
        slug, root = mock_integration
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


class TestClientAlphaRealRepairsClientSpecifics:
    """Specifics for the private real Client Alpha repairs client connector."""

    def _manifest(self):
        return _load_manifest(
            _CLIENT_DIR / "client_alpha_repairs" / "manifest.yaml",
        )

    def test_tier_is_api(self):
        assert self._manifest().tier == "api"

    def test_has_required_credentials(self):
        names = {s.name for s in self._manifest().secrets}
        assert "CLIENT_ALPHA_REPAIRS_API_BASE_URL" in names
        assert "CLIENT_ALPHA_REPAIRS_API_TOKEN" in names

    def test_not_also_in_generic_root(self):
        assert not (
            _BUILTIN_DIR / "client_alpha_repairs" / "manifest.yaml"
        ).is_file(), (
            "Real Client Alpha connector must live only under client_packages/."
        )

    def test_has_repairs_snapshot_capability(self):
        ids = {c.id for c in self._manifest().capabilities}
        assert "repairs_snapshot" in ids


class TestClientEpsilonHomesComplianceClientSpecifics:
    """Specifics for the private real ClientEpsilon compliance connector."""

    def _manifest(self):
        return _load_manifest(
            _CLIENT_DIR / "clientepsilon_homes_compliance" / "manifest.yaml",
        )

    def test_tier_is_api(self):
        assert self._manifest().tier == "api"

    def test_has_required_sharepoint_configuration(self):
        names = {s.name for s in self._manifest().secrets}
        assert "CLIENTEPSILON_SHAREPOINT_SITE_ID" in names
        assert "CLIENTEPSILON_SHAREPOINT_DRIVE_ID" in names
        assert "CLIENTEPSILON_SHAREPOINT_ROOT_PATH" in names

    def test_not_also_in_generic_root(self):
        assert not (
            _BUILTIN_DIR / "clientepsilon_homes_compliance" / "manifest.yaml"
        ).is_file(), "Real ClientEpsilon connector must live only under client_packages/."

    def test_has_compliance_snapshot_capability(self):
        ids = {c.id for c in self._manifest().capabilities}
        assert "compliance_snapshot" in ids


class TestClientAlphaRepairsMockSpecifics:
    """Specifics for the deterministic Client Alpha repairs mock."""

    def _manifest(self):
        return _load_manifest(
            _MOCK_DIR / "client_alpha_repairs_mock" / "manifest.yaml",
        )

    def test_tier_is_api(self):
        assert self._manifest().tier == "api"

    def test_no_required_credentials(self):
        for s in self._manifest().secrets:
            assert (
                s.required is False
            ), f"Mock package secret '{s.name}' must not be required."

    def test_capability_advertises_repairs_snapshot(self):
        ids = {c.id for c in self._manifest().capabilities}
        assert "repairs_snapshot" in ids


class TestClientEpsilonHomesComplianceMockSpecifics:
    """Specifics for the deterministic ClientEpsilon compliance demo mock."""

    def _manifest(self):
        return _load_manifest(
            _MOCK_DIR / "clientepsilon_homes_compliance_mock" / "manifest.yaml",
        )

    def test_tier_is_api(self):
        assert self._manifest().tier == "api"

    def test_no_required_credentials(self):
        for s in self._manifest().secrets:
            assert (
                s.required is False
            ), f"Mock package secret '{s.name}' must not be required."

    def test_declares_demo_site(self):
        manifest = self._manifest()
        assert manifest.demo_site is not None
        assert manifest.demo_site.dir == "demo_site"

    def test_capability_advertises_compliance_snapshot(self):
        ids = {c.id for c in self._manifest().capabilities}
        assert "compliance_snapshot" in ids
        assert "sharepoint_reasoning_scan" in ids
