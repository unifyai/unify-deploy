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
from unity.guidance_manager.types.guidance import Guidance
from unity_deploy.assistant_deployments.integrations.types import (
    IntegrationManifest,
    MCPServerConfig,
    SecretSchema,
)
from unity.secret_manager.types import Secret


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
