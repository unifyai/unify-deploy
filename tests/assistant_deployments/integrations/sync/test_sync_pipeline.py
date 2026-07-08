"""End-to-end tests verifying integration data flows into real managers.

These tests exercise the full path: manifest -> loader -> guidance.jsonl /
typed Secret models -> manager CRUD. They use the real Unity test
infrastructure (Unify context, actual manager instances) rather than mocks.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from tests.helpers import _handle_project
from unify.guidance_manager.custom_guidance import (
    GUIDANCE_JSONL_FILENAME,
    collect_custom_guidance,
)
from unify.guidance_manager.guidance_manager import GuidanceManager
from unity_deploy.assistant_deployments.integrations.loader import (
    LoadedIntegration,
    load_integration,
)
from unity_deploy.assistant_deployments.integrations.types import (
    IntegrationManifest,
    SecretSchema,
)
from unify.secret_manager.secret_manager import SecretManager

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
    lines = [
        json.dumps(
            {
                "key": "repo_lookup",
                "title": "Repo Lookup",
                "content": "How to look up GitHub repositories.",
            },
        ),
        json.dumps(
            {
                "key": "issue_triage",
                "title": "Issue Triage",
                "content": "How to triage issues on GitHub.",
            },
        ),
    ]
    (guidance_dir / GUIDANCE_JSONL_FILENAME).write_text("\n".join(lines) + "\n")

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


class TestLoaderTypedOutput:
    def test_load_guidance_returns_source_dicts(self, tmp_path: Path):
        guidance_dir = tmp_path / "guidance"
        guidance_dir.mkdir()
        (guidance_dir / GUIDANCE_JSONL_FILENAME).write_text(
            json.dumps(
                {
                    "key": "repo_lookup",
                    "title": "Repo Lookup",
                    "content": "Content.",
                },
            )
            + "\n",
        )
        entries = collect_custom_guidance(path=guidance_dir)
        assert "repo_lookup" in entries
        assert entries["repo_lookup"]["title"] == "Repo Lookup"

    def test_load_integration_returns_guidance_dir(self, github_integration):
        manifest, root = github_integration
        loaded = load_integration(manifest, root)
        assert isinstance(loaded, LoadedIntegration)
        assert loaded.guidance_dir is not None
        entries = collect_custom_guidance(path=loaded.guidance_dir)
        assert len(entries) == 2


@_handle_project
@pytest.mark.asyncio
async def test_loaded_guidance_syncs_to_manager(github_integration):
    manifest, root = github_integration
    loaded = load_integration(manifest, root)
    entries = collect_custom_guidance(path=loaded.guidance_dir)

    gm = GuidanceManager()
    gm.clear()
    changed = gm.sync_custom(source_guidance=entries)
    assert changed is True

    rows = gm.filter(filter="custom_hash != None", limit=100)
    titles = {row.title for row in rows}
    assert "Repo Lookup" in titles
    assert "Issue Triage" in titles

    gm.clear()


@_handle_project
@pytest.mark.asyncio
async def test_integration_secrets_sync_to_manager(github_integration):
    manifest, root = github_integration
    loaded = load_integration(manifest, root)
    suffix = uuid4().hex

    sm = SecretManager()
    for secret in loaded.secret_entries:
        result = sm._create_secret(
            name=f"{secret.name}_{suffix}",
            value=secret.value or "placeholder",
            description=secret.description,
        )
        assert result["outcome"] == "secret created"

    for secret in loaded.secret_entries:
        name = f"{secret.name}_{suffix}"
        rows = sm._filter_secrets(filter=f"name == '{name}'")
        assert rows and rows[0].name == name

    sm.clear()
