"""Tests for deployment-level integration activation."""

from __future__ import annotations

import logging

from unity_deploy.assistant_deployments.clients import (
    ClientDeploymentEntry,
    ResolvedAssistantDeployment,
    _spec_to_resolved,
)
from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig
from unity_deploy.assistant_deployments.deployment_types import DeploymentMapping
from unity_deploy.assistant_deployments.deployment_types import DeploymentSpec
from unity_deploy.assistant_deployments.deployment_types import DeploymentTarget
from unity_deploy.assistant_deployments.deployment_types import SeedLayer
from unity_deploy.assistant_deployments.integrations.activation import (
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
