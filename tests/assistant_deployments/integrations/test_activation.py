"""Tests for deployment-level integration activation."""

from __future__ import annotations

import json
import logging

from droid_deploy.assistant_deployments.clients import (
    ClientDeploymentEntry,
    ResolvedAssistantDeployment,
    _spec_to_resolved,
)
from droid_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from droid_deploy.assistant_deployments.deployment_types import DeploymentMapping
from droid_deploy.assistant_deployments.deployment_types import DeploymentSpec
from droid_deploy.assistant_deployments.deployment_types import DeploymentTarget
from droid_deploy.assistant_deployments.deployment_types import SeedLayer
from droid_deploy.assistant_deployments.integrations.activation import (
    expand_integrations,
)


def _empty_resolved(*, integrations: list[str]) -> ResolvedAssistantDeployment:
    return ResolvedAssistantDeployment(
        config=ActorConfig(),
        environments=[],
        function_dirs=[],
        venv_dirs=[],
        contacts=[],
        guidance=[],
        knowledge={},
        blacklist=[],
        secrets=[],
        integrations=integrations,
    )


def test_expand_github_integration_adds_seed_and_functions():
    resolved = _empty_resolved(integrations=["github"])

    expanded = expand_integrations(resolved)

    assert expanded.function_dirs
    assert any(path.name == "functions" for path in expanded.function_dirs)
    assert expanded.guidance
    assert {g.title for g in expanded.guidance} >= {"Repo Lookup", "Issue Triage"}
    assert {s.name for s in expanded.secrets} == {"GITHUB_TOKEN"}


def test_expand_fetch_mcp_integration_adds_mcp_config():
    resolved = _empty_resolved(integrations=["fetch_mcp"])

    expanded = expand_integrations(resolved)

    assert not expanded.function_dirs
    assert expanded.guidance
    assert len(expanded.mcp_configs) == 1
    assert expanded.mcp_configs[0].command == "npx"


def test_native_package_expansion_survives_provider_backed_sync_model():
    """Native packages still expand through droid-deploy assets, not provider rows."""

    resolved = _empty_resolved(
        integrations=["github", "fetch_mcp", "client_alpha_repairs_mock"],
    )

    expanded = expand_integrations(resolved, include_mock_packages=True)

    registry_by_slug = {row["slug"]: row for row in expanded.integration_registry}
    assert sorted(registry_by_slug) == [
        "fetch_mcp",
        "github",
        "client_alpha_repairs_mock",
    ]
    assert any(
        path.name == "functions" and path.parent.name == "github"
        for path in expanded.function_dirs
    )
    assert any(
        path.name == "functions" and path.parent.name == "client_alpha_repairs_mock"
        for path in expanded.function_dirs
    )
    assert len(expanded.mcp_configs) == 1
    assert expanded.mcp_configs[0].command == "npx"
    assert "@modelcontextprotocol/server-fetch" in expanded.mcp_configs[0].args
    assert expanded.scenarios
    assert {guidance.title for guidance in expanded.guidance} >= {
        "Repo Lookup",
        "Pilot Usage",
    }


def test_native_api_browser_tier_package_metadata_survives_provider_backed_sync_model():
    """Native packages can still ship API/browser-tier functions and guidance."""

    resolved = _empty_resolved(integrations=["matterport"])

    expanded = expand_integrations(resolved)

    assert any(
        path.name == "functions" and path.parent.name == "matterport"
        for path in expanded.function_dirs
    )
    guidance_titles = {guidance.title for guidance in expanded.guidance}
    assert {
        "Matterport Overview",
        "Matterport Setup",
        "Matterport Sync Runbook",
        "Matterport Unit Linking",
    }.issubset(guidance_titles)

    registry = {row["slug"]: row for row in expanded.integration_registry}
    matterport = registry["matterport"]
    function_names = set(json.loads(matterport["function_names_json"]))
    guidance_names = set(json.loads(matterport["guidance_titles_json"]))
    assert {
        "matterport_graphql_query",
        "generate_matterport_embed_url",
        "run_matterport_sync_tick",
    }.issubset(function_names)
    assert {"Matterport Overview", "Matterport Tier Gating"}.issubset(guidance_names)
    assert matterport["tier"] == "api"


def test_seed_layer_integrations_merge_with_spec_integrations():
    spec = DeploymentSpec(
        name="test",
        actor_config=ActorConfig(),
        integrations=["github"],
    )
    entry = ClientDeploymentEntry(
        mapping=DeploymentMapping(
            targets=[DeploymentTarget(scope="default", deployment="test")],
        ),
        specs={"test": spec},
        layers={
            "assistant:123": SeedLayer(
                integrations=["fetch_mcp", "github"],
            ),
        },
    )

    resolved = _spec_to_resolved(
        spec,
        entry,
        client_name="test",
        assistant_id=123,
    )

    assert resolved.integrations == ["github", "fetch_mcp"]


def test_unknown_integration_slug_logs_warning(caplog):
    resolved = _empty_resolved(integrations=["does_not_exist"])

    with caplog.at_level(logging.WARNING):
        expanded = expand_integrations(resolved)

    assert expanded.integrations == ["does_not_exist"]
    assert not expanded.function_dirs
    assert "does_not_exist" in caplog.text


def test_expand_seeds_integration_registry_with_one_row_per_slug():
    """``ResolvedAssistantDeployment.integration_registry`` should carry one
    row per loaded integration, ready for ``_sync_integration_registry`` to
    push into the ``Integrations/Manifests`` DataManager context."""
    resolved = _empty_resolved(integrations=["github", "fetch_mcp"])

    expanded = expand_integrations(resolved)

    slugs = sorted(row["slug"] for row in expanded.integration_registry)
    assert slugs == ["fetch_mcp", "github"]
    by_slug = {row["slug"]: row for row in expanded.integration_registry}
    # Required vs optional secrets are split so the runtime detection can
    # distinguish "must have to be enabled" from "informational hint".
    assert "required_secrets_json" in by_slug["github"]
    assert "optional_secrets_json" in by_slug["github"]
    # Description carries through so the runtime can print human-readable
    # status without a follow-up lookup.
    assert by_slug["github"]["description"]


def test_expand_idempotent_on_repeat_invocation():
    """Re-running ``expand_integrations`` over the same resolved object must
    not duplicate registry rows."""
    resolved = _empty_resolved(integrations=["github"])
    expand_integrations(resolved)
    expand_integrations(resolved)
    slugs = [row["slug"] for row in resolved.integration_registry]
    assert slugs == ["github"]


def test_unknown_slug_does_not_emit_registry_row(caplog):
    resolved = _empty_resolved(integrations=["does_not_exist"])
    with caplog.at_level(logging.WARNING):
        expanded = expand_integrations(resolved)
    assert expanded.integration_registry == []
