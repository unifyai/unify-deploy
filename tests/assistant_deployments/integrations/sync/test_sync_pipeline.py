"""End-to-end tests verifying integration data flows into real managers.

These tests exercise the full path: manifest -> loader -> typed Guidance/Secret
models -> manager CRUD. They use the real Unity test infrastructure (Unify
context, actual manager instances) rather than mocks.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from tests.helpers import _handle_project
from unify.guidance_manager.guidance_manager import GuidanceManager
from unify.guidance_manager.types.guidance import Guidance
from unity_deploy.assistant_deployments.integrations.loader import (
    LoadedIntegration,
    _load_guidance,
    load_integration,
)
from unity_deploy.assistant_deployments.integrations.types import (
    IntegrationManifest,
    SecretSchema,
)
from unify.secret_manager.secret_manager import SecretManager
from unify.secret_manager.types import Secret

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def github_integration(tmp_path: Path) -> tuple[IntegrationManifest, Path]:
    """Create a minimal GitHub-like integration package on disk."""
    root = tmp_path / "github"
    root.mkdir()
    (root / "__init__.py").write_text("")

    functions_dir = root / "functions"
    functions_dir.mkdir()
    (functions_dir / "__init__.py").write_text("")
    (functions_dir / "users.py").write_text(
        "async def get_user(): pass\n",
    )

    guidance_dir = root / "guidance"
    guidance_dir.mkdir()
    (guidance_dir / "repo_lookup.md").write_text(
        "# Repo Lookup\n\nHow to look up GitHub repositories.",
    )
    (guidance_dir / "issue_triage.md").write_text(
        "# Issue Triage\n\nHow to triage issues on GitHub.",
    )

    manifest = IntegrationManifest(
        name="GitHub",
        slug="github",
        sector="developer-tools",
        tier="api",
        description="GitHub REST API integration for testing",
        secrets=[
            SecretSchema(
                name="GITHUB_TOKEN",
                description="Personal access token",
                required=False,
            ),
        ],
    )

    (root / "manifest.yaml").write_text(yaml.dump(manifest.model_dump(mode="json")))
    return manifest, root


# ---------------------------------------------------------------------------
# Typed output tests
# ---------------------------------------------------------------------------


class TestTypedOutputs:
    """Verify that the loader produces canonical Guidance/Secret models."""

    def test_load_guidance_returns_guidance_models(self, tmp_path: Path):
        guidance_dir = tmp_path / "guidance"
        guidance_dir.mkdir()
        (guidance_dir / "repo_lookup.md").write_text("# Repo Lookup\n\nContent.")
        (guidance_dir / "issue_triage.md").write_text("# Issue Triage\n\nContent.")

        entries = _load_guidance(guidance_dir)
        assert len(entries) == 2
        assert all(isinstance(e, Guidance) for e in entries)
        titles = {e.title for e in entries}
        assert "Repo Lookup" in titles
        assert "Issue Triage" in titles

    def test_load_integration_produces_typed_entries(self, github_integration):
        manifest, root = github_integration
        loaded = load_integration(manifest, root)

        assert isinstance(loaded, LoadedIntegration)
        assert all(isinstance(g, Guidance) for g in loaded.guidance_entries)
        assert all(isinstance(s, Secret) for s in loaded.secret_entries)
        assert len(loaded.guidance_entries) == 2
        assert len(loaded.secret_entries) == 1

    def test_secret_entries_have_empty_values(self, github_integration):
        manifest, root = github_integration
        loaded = load_integration(manifest, root)

        for s in loaded.secret_entries:
            assert s.value == ""
            assert s.name == "GITHUB_TOKEN"
            assert isinstance(s.description, str)
            assert len(s.description) > 0


# ---------------------------------------------------------------------------
# Real manager sync tests
# ---------------------------------------------------------------------------


class TestGuidanceSync:
    """Verify that loaded Guidance models can be synced to GuidanceManager."""

    @_handle_project
    def test_guidance_round_trip(self):
        gm = GuidanceManager()

        guidance_entries = [
            Guidance(
                title="Integration Test Guide",
                content="Step-by-step integration test content.",
            ),
            Guidance(
                title="Second Guide",
                content="Another piece of guidance for testing.",
            ),
        ]

        created_ids = []
        for g in guidance_entries:
            result = gm.add_guidance(title=g.title, content=g.content)
            created_ids.append(result["details"]["guidance_id"])

        for i, g in enumerate(guidance_entries):
            rows = gm.filter(filter=f"guidance_id == {created_ids[i]}")
            assert rows and len(rows) == 1
            assert rows[0].title == g.title
            assert rows[0].content == g.content

    @_handle_project
    def test_loaded_guidance_syncs_to_manager(self, tmp_path: Path):
        guidance_dir = tmp_path / "guidance"
        guidance_dir.mkdir()
        (guidance_dir / "test_guide.md").write_text(
            "# Test Guide\n\nDetailed guidance content for the test.",
        )

        entries = _load_guidance(guidance_dir)
        assert len(entries) == 1
        assert isinstance(entries[0], Guidance)

        gm = GuidanceManager()
        result = gm.add_guidance(title=entries[0].title, content=entries[0].content)
        gid = result["details"]["guidance_id"]

        rows = gm.filter(filter=f"guidance_id == {gid}")
        assert rows and rows[0].title == "Test Guide"


class TestSecretSync:
    """Verify that loaded Secret models can be synced to SecretManager."""

    @_handle_project
    def test_secret_round_trip(self):
        sm = SecretManager()
        suffix = uuid4().hex

        secret_entries = [
            Secret(
                name=f"SYNC_TEST_KEY_1_{suffix}",
                value="val1",
                description="First test key",
            ),
            Secret(
                name=f"SYNC_TEST_KEY_2_{suffix}",
                value="val2",
                description="Second test key",
            ),
        ]

        for s in secret_entries:
            result = sm._create_secret(
                name=s.name,
                value=s.value,
                description=s.description,
            )
            assert result["outcome"] == "secret created"

        for s in secret_entries:
            rows = sm._filter_secrets(filter=f"name == '{s.name}'")
            assert rows and len(rows) == 1
            assert rows[0].name == s.name
            assert rows[0].value == ""  # read path never exposes values

    @_handle_project
    def test_integration_secrets_sync_to_manager(self, github_integration):
        manifest, root = github_integration
        loaded = load_integration(manifest, root)
        suffix = uuid4().hex

        sm = SecretManager()
        for s in loaded.secret_entries:
            sm._create_secret(
                name=f"{s.name}_{suffix}",
                value=s.value or "placeholder",
                description=s.description,
            )

        for s in loaded.secret_entries:
            name = f"{s.name}_{suffix}"
            rows = sm._filter_secrets(filter=f"name == '{name}'")
            assert rows and rows[0].name == name
