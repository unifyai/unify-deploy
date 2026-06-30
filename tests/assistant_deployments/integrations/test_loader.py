"""Symbolic tests for integration loading and aggregation.

Tests that the loader correctly converts manifests into the data
structures the sync pipeline expects.
"""

import pytest
from pathlib import Path

import yaml

from unity_deploy.assistant_deployments.integrations.loader import (
    AggregatedIntegrations,
    load_integration,
    load_integrations,
    _load_guidance,
)
from unify.guidance_manager.types.guidance import Guidance
from unity_deploy.assistant_deployments.integrations.types import (
    Capability,
    IntegrationManifest,
    MCPServerConfig,
    SecretSchema,
)
from unify.secret_manager.types import Secret


@pytest.fixture
def github_integration(tmp_path: Path) -> tuple[IntegrationManifest, Path]:
    """Create a minimal GitHub-like integration package."""
    root = tmp_path / "github"
    root.mkdir()
    (root / "__init__.py").write_text("")

    functions_dir = root / "functions"
    functions_dir.mkdir()
    (functions_dir / "__init__.py").write_text("")
    (functions_dir / "users.py").write_text(
        "async def get_user(): pass\n" "async def get_user_repos(): pass\n",
    )

    guidance_dir = root / "guidance"
    guidance_dir.mkdir()
    (guidance_dir / "repo_lookup.md").write_text(
        "# Repo Lookup\n\nHow to look up GitHub repositories.",
    )

    manifest = IntegrationManifest(
        name="GitHub",
        slug="github",
        sector="developer-tools",
        tier="api",
        description="GitHub REST API integration",
        secrets=[
            SecretSchema(
                name="GITHUB_TOKEN",
                description="Personal access token",
                required=False,
            ),
        ],
        requirements=["httpx"],
    )

    (root / "manifest.yaml").write_text(yaml.dump(manifest.model_dump(mode="json")))
    return manifest, root


@pytest.fixture
def mcp_integration(tmp_path: Path) -> tuple[IntegrationManifest, Path]:
    """Create a minimal MCP-tier integration package (tmp_path-based)."""
    root = tmp_path / "test_mcp"
    root.mkdir()
    (root / "__init__.py").write_text("")

    guidance_dir = root / "guidance"
    guidance_dir.mkdir()
    (guidance_dir / "usage.md").write_text(
        "# Usage\n\nHow to use this MCP server.",
    )

    manifest = IntegrationManifest(
        name="Test MCP",
        slug="test_mcp",
        sector="test",
        tier="mcp",
        description="Test MCP integration",
        mcp=MCPServerConfig(
            command="echo",
            args=["hello"],
        ),
    )

    (root / "manifest.yaml").write_text(yaml.dump(manifest.model_dump(mode="json")))
    return manifest, root


class TestLoadGuidance:
    def test_loads_markdown_files(self, tmp_path: Path):
        guidance_dir = tmp_path / "guidance"
        guidance_dir.mkdir()
        (guidance_dir / "repo_lookup.md").write_text("# Repo Lookup\n\nContent here.")
        (guidance_dir / "issue_triage.md").write_text("# Issue Triage\n\nTriage info.")

        entries = _load_guidance(guidance_dir)
        assert len(entries) == 2
        assert all(isinstance(e, Guidance) for e in entries)
        titles = {e.title for e in entries}
        assert "Repo Lookup" in titles
        assert "Issue Triage" in titles

    def test_title_from_filename(self, tmp_path: Path):
        guidance_dir = tmp_path / "guidance"
        guidance_dir.mkdir()
        (guidance_dir / "file_operations.md").write_text("Content")

        entries = _load_guidance(guidance_dir)
        assert entries[0].title == "File Operations"

    def test_empty_dir(self, tmp_path: Path):
        guidance_dir = tmp_path / "guidance"
        guidance_dir.mkdir()
        assert _load_guidance(guidance_dir) == []


class TestLoadIntegration:
    def test_loads_functions_dir(self, github_integration):
        manifest, root = github_integration
        loaded = load_integration(manifest, root)
        assert loaded.function_dir is not None
        assert loaded.function_dir.name == "functions"

    def test_loads_guidance(self, github_integration):
        manifest, root = github_integration
        loaded = load_integration(manifest, root)
        assert len(loaded.guidance_entries) == 1
        assert isinstance(loaded.guidance_entries[0], Guidance)
        assert loaded.guidance_entries[0].title == "Repo Lookup"

    def test_loads_secrets(self, github_integration):
        manifest, root = github_integration
        loaded = load_integration(manifest, root)
        assert len(loaded.secret_entries) == 1
        assert all(isinstance(s, Secret) for s in loaded.secret_entries)
        assert loaded.secret_entries[0].name == "GITHUB_TOKEN"
        assert loaded.secret_entries[0].value == ""

    def test_no_url_mapping_without_demo_site(self, github_integration):
        manifest, root = github_integration
        loaded = load_integration(manifest, root)
        assert loaded.url_mapping is None

    def test_loads_mcp_config(self, mcp_integration):
        manifest, root = mcp_integration
        loaded = load_integration(manifest, root)
        assert loaded.mcp_config is not None
        assert loaded.mcp_config.command == "echo"

    def test_no_mcp_for_api_tier(self, github_integration):
        manifest, root = github_integration
        loaded = load_integration(manifest, root)
        assert loaded.mcp_config is None

    def test_registry_row_minimal(self, github_integration):
        manifest, root = github_integration
        loaded = load_integration(manifest, root)
        assert loaded.registry_row is not None
        assert loaded.registry_row["slug"] == "github"
        assert loaded.registry_row["label"] == "GitHub"
        assert loaded.registry_row["category"] == "developer-tools"
        assert loaded.registry_row["tier"] == "api"
        # GITHUB_TOKEN is required=False in the fixture.
        import json

        assert json.loads(loaded.registry_row["required_secrets_json"]) == []
        assert json.loads(loaded.registry_row["optional_secrets_json"]) == [
            "GITHUB_TOKEN",
        ]
        # No capabilities → no functions/guidance in the row.
        assert json.loads(loaded.registry_row["function_names_json"]) == []
        assert json.loads(loaded.registry_row["guidance_titles_json"]) == []
        assert json.loads(loaded.registry_row["capability_ids_json"]) == []


class TestRegistryRowFromCapabilities:
    """Registry row projects capability function/guidance maps for runtime detection."""

    def _make_pkg(self, tmp_path: Path) -> tuple[IntegrationManifest, Path]:
        root = tmp_path / "demo"
        root.mkdir()
        (root / "__init__.py").write_text("")
        (root / "functions").mkdir()
        (root / "functions" / "__init__.py").write_text("")
        (root / "guidance").mkdir()
        (root / "guidance" / "demo_overview.md").write_text("# Overview")
        (root / "guidance" / "demo_workflows.md").write_text("# Workflows")

        manifest = IntegrationManifest(
            name="Demo",
            slug="demo",
            sector="crm",
            tier="api",
            description="A demo integration",
            secrets=[
                SecretSchema(name="DEMO_API_KEY", description="API key", required=True),
                SecretSchema(
                    name="DEMO_OPTIONAL",
                    description="Optional",
                    required=False,
                ),
            ],
            capabilities=[
                Capability(
                    id="contacts",
                    name="Contacts",
                    description="Lookup",
                    functions=["get_contact", "list_contacts"],
                    guidance=["demo_overview"],
                ),
                Capability(
                    id="workflows",
                    name="Workflows",
                    description="Run",
                    functions=["run_workflow"],
                    guidance=["demo_workflows"],
                ),
            ],
        )
        (root / "manifest.yaml").write_text(yaml.dump(manifest.model_dump(mode="json")))
        return manifest, root

    def test_required_and_optional_secrets_split(self, tmp_path):
        manifest, root = self._make_pkg(tmp_path)
        loaded = load_integration(manifest, root)
        import json

        row = loaded.registry_row
        assert json.loads(row["required_secrets_json"]) == ["DEMO_API_KEY"]
        assert json.loads(row["optional_secrets_json"]) == ["DEMO_OPTIONAL"]

    def test_function_names_unioned_across_capabilities(self, tmp_path):
        manifest, root = self._make_pkg(tmp_path)
        loaded = load_integration(manifest, root)
        import json

        # Sorted in registry to make hash-based seed sync deterministic.
        assert json.loads(loaded.registry_row["function_names_json"]) == [
            "get_contact",
            "list_contacts",
            "run_workflow",
        ]

    def test_guidance_titles_match_loader_title_transform(self, tmp_path):
        """Registry titles must match what ``_load_guidance`` writes into
        GuidanceManager — otherwise the runtime can't resolve them at lookup."""
        manifest, root = self._make_pkg(tmp_path)
        loaded = load_integration(manifest, root)
        import json

        seeded_titles = sorted(g.title for g in loaded.guidance_entries)
        registry_titles = json.loads(loaded.registry_row["guidance_titles_json"])
        assert registry_titles == seeded_titles

    def test_capability_ids_preserved_in_order(self, tmp_path):
        manifest, root = self._make_pkg(tmp_path)
        loaded = load_integration(manifest, root)
        import json

        assert json.loads(loaded.registry_row["capability_ids_json"]) == [
            "contacts",
            "workflows",
        ]


class TestLoadIntegrations:
    def test_loads_multiple_by_slug(
        self,
        tmp_path,
        github_integration,
        mcp_integration,
    ):
        _, github_root = github_integration
        _, mcp_root = mcp_integration
        search_path = github_root.parent

        result = load_integrations(
            ["github", "test_mcp"],
            [search_path],
        )
        assert isinstance(result, AggregatedIntegrations)
        assert len(result.function_dirs) == 1  # only github has functions
        assert len(result.guidance) == 2
        assert len(result.secrets) == 1
        assert len(result.mcp_configs) == 1

    def test_missing_slug_logged(self, tmp_path):
        result = load_integrations(["nonexistent"], [tmp_path])
        assert result.function_dirs == []
        assert result.guidance == []

    def test_aggregates_registry_rows_in_slug_order(
        self,
        github_integration,
        mcp_integration,
    ):
        _, github_root = github_integration
        _, mcp_root = mcp_integration

        result = load_integrations(
            ["github", "test_mcp"],
            [github_root.parent],
        )
        slugs = [row["slug"] for row in result.registry_rows]
        # Both manifests pass minimal validation (no capabilities); we still
        # emit a row apiece so the runtime can list them in setup-completeness
        # output even when zero capabilities exist.
        assert slugs == ["github", "test_mcp"]

    def test_first_search_path_wins(self, tmp_path):
        path1 = tmp_path / "path1" / "test_pkg"
        path1.mkdir(parents=True)
        (path1 / "__init__.py").write_text("")
        (path1 / "manifest.yaml").write_text(
            yaml.dump(
                {
                    "name": "First",
                    "slug": "test_pkg",
                    "sector": "test",
                    "tier": "api",
                    "description": "First path wins",
                },
            ),
        )

        path2 = tmp_path / "path2" / "test_pkg"
        path2.mkdir(parents=True)
        (path2 / "__init__.py").write_text("")
        (path2 / "manifest.yaml").write_text(
            yaml.dump(
                {
                    "name": "Second",
                    "slug": "test_pkg",
                    "sector": "test",
                    "tier": "api",
                    "description": "Second path",
                },
            ),
        )

        result = load_integrations(
            ["test_pkg"],
            [tmp_path / "path1", tmp_path / "path2"],
        )
        assert len(result.guidance) == 0
