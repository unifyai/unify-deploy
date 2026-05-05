"""Wake-time startup configuration assembly for assistant deployments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from unity_deploy.assistant_deployments.clients import ResolvedAssistantDeployment


@dataclass(frozen=True)
class StartupIdentity:
    """Concrete assistant identity available during startup hook execution."""

    assistant_id: str
    user_id: str
    org_id: int | None = None
    team_ids: tuple[int, ...] = ()


def resolve_startup_spec(identity: StartupIdentity) -> ResolvedAssistantDeployment:
    """Resolve the assistant deployment spec without side effects."""

    from unity_deploy.assistant_deployments.clients import resolve

    return resolve(
        org_id=identity.org_id,
        team_ids=list(identity.team_ids) or None,
        user_id=identity.user_id,
        assistant_id=(
            int(identity.assistant_id) if str(identity.assistant_id).isdigit() else None
        ),
    )


def expand_startup_integrations(
    resolved: ResolvedAssistantDeployment,
) -> ResolvedAssistantDeployment:
    """Expand integration slugs into in-memory startup assets."""

    from unity_deploy.assistant_deployments.integrations.activation import (
        expand_integrations,
    )

    return expand_integrations(resolved)


def build_actor_startup_config(resolved: ResolvedAssistantDeployment) -> dict[str, Any]:
    """Build the config payload consumed by Unity actor initialization."""

    from unity_deploy.runtime import get_runtime_backend_overrides

    config = resolved.config
    url_mappings = dict(config.url_mappings or {})
    url_mappings.update(resolved.url_mappings)
    return {
        "environments": resolved.environments,
        "url_mappings": url_mappings or None,
        "runtime_backends": get_runtime_backend_overrides(),
        "actor_kwargs": {
            k: v
            for k, v in {
                "can_compose": config.can_compose,
                "can_store": config.can_store,
                "timeout": config.timeout,
                "model": config.model,
                "prompt_caching": config.prompt_caching,
                "guidelines": config.guidelines,
            }.items()
            if v is not None
        },
    }
