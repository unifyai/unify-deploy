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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar, TYPE_CHECKING

from unity.guidance_manager.types.guidance import Guidance
from unity.secret_manager.types import Secret
from unity_deploy.customization.configs.types.actor_config import ActorConfig

if TYPE_CHECKING:
    from unity.actor.environments.base import BaseEnvironment
    from unity_deploy.customization.deployment_types import (
        DeploymentMapping,
        DeploymentSpec,
        SeedLayer,
    )

T = TypeVar("T")

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
    guidance: list[Guidance]
    knowledge: dict[str, dict[str, Any]]
    blacklist: list[dict[str, Any]]
    secrets: list[Secret]


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
    layers: dict[str, SeedLayer] = field(default_factory=dict)


_CLIENT_DEPLOYMENTS: dict[str, ClientDeploymentEntry] = {}


# ---------------------------------------------------------------------------
# Seed-layer merge helpers
# ---------------------------------------------------------------------------


def _merge_by_key(
    base: list[T],
    overlay: list[T],
    key_fn: Callable[[T], str],
) -> list[T]:
    """Merge two record lists; overlay wins on key collision."""
    merged: dict[str, T] = {}
    for rec in base:
        merged[key_fn(rec)] = rec
    for rec in overlay:
        merged[key_fn(rec)] = rec
    return list(merged.values())


def _contact_key(r: dict) -> str:
    return f"{r.get('first_name', '')}|{r.get('surname', '')}".lower()


def _guidance_key(r: Guidance) -> str:
    return r.title


def _blacklist_key(r: dict) -> str:
    return f"{r.get('medium', '')}|{r.get('contact_detail', '')}"


def _secret_key(r: Secret) -> str:
    return r.name


def _merge_knowledge(
    base: dict[str, dict],
    overlay: dict[str, dict],
) -> dict[str, dict]:
    """Deep-merge knowledge tables.  Overlay columns and rows win on collision."""
    merged = {k: dict(v) for k, v in base.items()}
    for table_name, spec in overlay.items():
        if table_name not in merged:
            merged[table_name] = dict(spec)
            continue
        existing = merged[table_name]
        if spec.get("columns"):
            existing.setdefault("columns", {}).update(spec["columns"])
        if spec.get("description"):
            existing["description"] = spec["description"]
        seed_key = spec.get("seed_key") or existing.get("seed_key")
        if seed_key:
            existing["seed_key"] = seed_key
        if spec.get("rows"):
            all_rows = existing.get("rows", []) + spec["rows"]
            if seed_key:
                seen: dict[str, dict] = {}
                for row in all_rows:
                    seen[str(row.get(seed_key, id(row)))] = row
                existing["rows"] = list(seen.values())
            else:
                existing["rows"] = all_rows
    return merged


def _collect_layers(
    entry: ClientDeploymentEntry,
    *,
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> list["SeedLayer"]:
    """Return matching layers in scope order (org -> teams -> user -> assistant)."""
    layers = entry.layers
    if not layers:
        return []

    result: list[SeedLayer] = []
    if org_id is not None:
        layer = layers.get(f"org:{org_id}")
        if layer is not None:
            result.append(layer)
    if team_ids:
        for tid in sorted(team_ids):
            layer = layers.get(f"team:{tid}")
            if layer is not None:
                result.append(layer)
    if user_id is not None:
        layer = layers.get(f"user:{user_id}")
        if layer is not None:
            result.append(layer)
    if assistant_id is not None:
        layer = layers.get(f"assistant:{assistant_id}")
        if layer is not None:
            result.append(layer)
    return result


# ---------------------------------------------------------------------------
# Spec -> ResolvedCustomization
# ---------------------------------------------------------------------------


def _spec_to_resolved(
    spec: "DeploymentSpec",
    entry: ClientDeploymentEntry,
    *,
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> ResolvedCustomization:
    """Convert a :class:`DeploymentSpec` into a resolved result.

    Shared seed-data layers registered on *entry* are collected in
    scope order (org -> team -> user -> assistant) and merged onto
    the spec's seed data.  File-based secrets from ``.secrets.json``
    are applied last.
    """
    from unity_deploy.customization.secrets_file import load_secrets

    contacts: list[dict] = list(spec.contacts)
    guidance: list[Guidance] = list(spec.guidance)
    knowledge: dict[str, dict] = dict(spec.knowledge)
    blacklist: list[dict] = list(spec.blacklist)
    secrets: list[Secret] = list(spec.secrets)

    for layer in _collect_layers(
        entry,
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    ):
        if layer.contacts:
            contacts = _merge_by_key(contacts, layer.contacts, _contact_key)
        if layer.guidance:
            guidance = _merge_by_key(guidance, list(layer.guidance), _guidance_key)
        if layer.knowledge:
            knowledge = _merge_knowledge(knowledge, layer.knowledge)
        if layer.blacklist:
            blacklist = _merge_by_key(blacklist, layer.blacklist, _blacklist_key)
        if layer.secrets:
            secrets = _merge_by_key(secrets, list(layer.secrets), _secret_key)

    file_secrets = load_secrets(
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    )
    if file_secrets:
        file_secret_models = [Secret(**s) for s in file_secrets]
        secrets = _merge_by_key(secrets, file_secret_models, _secret_key)

    return ResolvedCustomization(
        config=spec.actor_config,
        environments=list(spec.environments),
        function_dirs=[spec.function_dir] if spec.function_dir else [],
        venv_dirs=[spec.venv_dir] if spec.venv_dir else [],
        contacts=contacts,
        guidance=guidance,
        knowledge=knowledge,
        blacklist=blacklist,
        secrets=secrets,
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
            entry,
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
