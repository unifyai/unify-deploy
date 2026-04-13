"""
Code-first client customization registry.

Client configs, environments, custom functions, and seed data are defined
in Python code under per-client subpackages (e.g. ``client_alpha/``).
Each subpackage declares environment-scoped deployment mappings and calls
:func:`~unity_deploy.customization.deployment_types.register_client` to
register its specs into the ``_CLIENT_DEPLOYMENTS`` registry.

During manager initialization, ``resolve()`` is called with the current
org_id / team_ids / user_id / assistant_id to produce a
``ResolvedCustomization`` by finding the matching client and returning
the matching deployment spec directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from unity_deploy.customization.configs.types.actor_config import ActorConfig

if TYPE_CHECKING:
    from unity.actor.environments.base import BaseEnvironment
    from unity_deploy.customization.deployment_types import (
        DeploymentMapping,
        DeploymentSpec,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Resolved result
# ---------------------------------------------------------------------------


@dataclass
class ResolvedCustomization:
    config: ActorConfig
    environments: list[BaseEnvironment]
    function_dirs: list[Path]
    venv_dirs: list[Path]
    contacts: list[dict[str, Any]]
    guidance: list[dict[str, Any]]
    knowledge: dict[str, dict[str, Any]]
    blacklist: list[dict[str, Any]]
    secrets: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Deployment registry
# ---------------------------------------------------------------------------


@dataclass
class ClientDeploymentEntry:
    """A registered client with its mapping and loaded deployment specs.

    ``default_org_id`` and ``default_user_id`` are metadata — they
    record which org/user this client is intended for but do **not**
    act as hard filters during resolution.  Routing is driven entirely
    by the :class:`DeploymentMapping` targets.
    """

    mapping: DeploymentMapping
    specs: dict[str, DeploymentSpec]
    default_org_id: int | None = None
    default_user_id: str | None = None
    environment: str | None = None


_CLIENT_DEPLOYMENTS: dict[str, ClientDeploymentEntry] = {}


def _spec_to_resolved(
    spec: "DeploymentSpec",
    *,
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> ResolvedCustomization:
    """Convert a :class:`DeploymentSpec` directly into a resolved result.

    File-based secrets from ``.secrets.json`` are merged with any
    code-defined secrets in the spec.
    """
    from unity_deploy.customization.secrets_file import load_secrets

    code_secrets = [e.model_dump() for e in spec.secrets]
    file_secrets = load_secrets(
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    )
    secrets_by_name: dict[str, dict[str, Any]] = {}
    for s in code_secrets:
        secrets_by_name[s["name"]] = s
    for s in file_secrets:
        secrets_by_name[s["name"]] = {**secrets_by_name.get(s["name"], {}), **s}

    return ResolvedCustomization(
        config=spec.actor_config,
        environments=list(spec.environments),
        function_dirs=[spec.function_dir] if spec.function_dir else [],
        venv_dirs=[spec.venv_dir] if spec.venv_dir else [],
        contacts=list(spec.contacts),
        guidance=[e.model_dump() for e in spec.guidance],
        knowledge=dict(spec.knowledge),
        blacklist=list(spec.blacklist),
        secrets=list(secrets_by_name.values()),
    )


def resolve_from_deployments(
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> ResolvedCustomization | None:
    """Walk ``_CLIENT_DEPLOYMENTS`` and return an isolated result if a match is found.

    Each registered client is checked in order:

    1. **Environment guardrail** — skip if registered for a different
       environment than what :func:`detect_environment` reports.
    2. **Mapping resolution** — walk the client's
       :class:`DeploymentMapping` to find the first matching target.
       If no target matches, move on to the next client.
    3. Return the spec converted to :class:`ResolvedCustomization`
       via :func:`_spec_to_resolved`.

    ``default_org_id`` and ``default_user_id`` on the entry are
    metadata only — routing is driven entirely by the mapping targets.

    Returns ``None`` when no client matches.
    """
    from unity_deploy.customization.deployment_types import (
        detect_environment,
        resolve_deployment_name,
    )

    current_env = detect_environment()

    for client_name, entry in _CLIENT_DEPLOYMENTS.items():
        if entry.environment is not None and entry.environment != current_env:
            logger.debug(
                "Skipping client '%s' — registered for '%s' but running in '%s'",
                client_name,
                entry.environment,
                current_env,
            )
            continue

        try:
            dep_name = resolve_deployment_name(
                entry.mapping,
                user_id=user_id,
                org_id=org_id,
                team_ids=team_ids,
                assistant_id=assistant_id,
            )
        except ValueError:
            continue

        spec = entry.specs[dep_name]
        return _spec_to_resolved(
            spec,
            org_id=org_id,
            team_ids=team_ids,
            user_id=user_id,
            assistant_id=assistant_id,
        )

    return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve(
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> ResolvedCustomization:
    """Resolve customizations for the given identity.

    Walks ``_CLIENT_DEPLOYMENTS`` looking for a client whose mapping
    targets match the session identity.  Returns the deployment spec
    as a :class:`ResolvedCustomization`.

    When no client matches, returns an empty default customization.
    """
    result = resolve_from_deployments(
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    )
    if result is not None:
        return result

    return ResolvedCustomization(
        config=ActorConfig(),
        environments=[],
        function_dirs=[],
        venv_dirs=[],
        contacts=[],
        guidance=[],
        knowledge={},
        blacklist=[],
        secrets=[],
    )


# ---------------------------------------------------------------------------
# Import client subpackages so they self-register.
# Add new clients here.
# ---------------------------------------------------------------------------

from . import client_alpha  # noqa: F401, E402

# TODO: Yasser has left the team.  Re-enable when a new ClientGamma deployment
# owner is assigned and _ENVIRONMENTS is populated in clientgamma/__init__.py.
# from . import clientgamma  # noqa: F401, E402
