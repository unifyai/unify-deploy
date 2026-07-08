"""
Code-first assistant deployment registry.

Client configs, environments, custom functions, and seed data are defined
in Python code under per-client subpackages (e.g. ``client_alpha/``).
Each subpackage declares environment-scoped deployment mappings and calls
:func:`~unity_deploy.assistant_deployments.deployment_types.register_client` to
register its specs into the ``_CLIENT_DEPLOYMENTS`` registry.

During manager initialization, ``resolve()`` is called with the current
org_id / team_ids / user_id / assistant_id to produce a
``ResolvedAssistantDeployment`` by finding the matching client and returning
the matching deployment spec directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar, TYPE_CHECKING

from unify.secret_manager.types import Secret
from unity_deploy.assistant_deployments.configs.types.actor_config import ActorConfig

if TYPE_CHECKING:
    from unify.actor.environments.base import BaseEnvironment
    from unity_deploy.assistant_deployments.deployment_types import (
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
class ResolvedAssistantDeployment:
    config: ActorConfig
    environments: list[BaseEnvironment]
    function_dirs: list[Path]
    venv_dirs: list[Path]
    guidance_dirs: list[Path]
    contacts: list[dict[str, Any]]
    knowledge: dict[str, dict[str, Any]]
    blacklist: list[dict[str, Any]]
    secrets: list[Secret]
    integrations: list[str] = field(default_factory=list)
    integration_registry: list[dict[str, Any]] = field(default_factory=list)
    """One row per enabled integration, populated by ``expand_integrations``.

    Seeded into the ``Integrations/Manifests`` DataManager context by
    ``_sync_integration_registry`` and consumed at runtime by
    ``unify.integration_status`` to compute which integrations have working
    credentials.  See ``integrations/loader.py:_build_registry_row``."""
    mcp_configs: list[Any] = field(default_factory=list)
    url_mappings: dict[str, str] = field(default_factory=dict)
    console_config: dict[str, Any] | None = None
    scenarios: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Deployment registry
# ---------------------------------------------------------------------------


@dataclass
class ClientDeploymentEntry:
    """A registered client with its mapping and loaded deployment specs.

    Routing is driven entirely by :class:`DeploymentMapping` targets —
    scoping (org-wide, user-wide, assistant-specific) is expressed
    through :class:`DeploymentTarget` entries.
    """

    mapping: DeploymentMapping
    specs: dict[str, DeploymentSpec]
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


def _append_guidance_dir(dirs: list[Path], guidance_dir: Path | None) -> list[Path]:
    if guidance_dir is None:
        return dirs
    resolved = str(Path(guidance_dir).resolve())
    if any(str(Path(existing).resolve()) == resolved for existing in dirs):
        return dirs
    return [*dirs, Path(guidance_dir)]


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


def _merge_integrations(base: list[str], overlay: list[str]) -> list[str]:
    """Append integration slugs while preserving order and removing duplicates."""
    result: list[str] = []
    for slug in [*base, *overlay]:
        if slug not in result:
            result.append(slug)
    return result


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
# Spec -> ResolvedAssistantDeployment
# ---------------------------------------------------------------------------


def _spec_to_resolved(
    spec: "DeploymentSpec",
    entry: ClientDeploymentEntry,
    *,
    client_name: str,
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> ResolvedAssistantDeployment:
    """Convert a :class:`DeploymentSpec` into a resolved result.

    Shared seed-data layers registered on *entry* are collected in
    scope order (org -> team -> user -> assistant) and merged onto
    the spec's seed data.  File-based secrets from ``.secrets.json``
    are applied last.

    Scenario activations declared on the deployment spec and on each
    matching seed layer are materialised here: their generic templates
    are loaded from disk, placeholder fields substituted with the
    per-client values, validated as :class:`ScenarioSpec`, and appended
    to ``resolved.scenarios``.  Env-var overlay produced by activations
    is merged into the secrets bundle.
    """
    from unity_deploy.assistant_deployments.deployment_types import (
        resolve_deployment_name,
    )
    from unity_deploy.assistant_deployments.scenarios.loader import (
        materialise_scenario_activations,
    )
    from unity_deploy.assistant_deployments.scenarios.types import ScenarioActivation
    from unity_deploy.assistant_deployments.secrets_file import load_secrets

    contacts: list[dict] = list(spec.contacts)
    guidance_dirs: list[Path] = []
    if spec.guidance_dir is not None:
        guidance_dirs = _append_guidance_dir(guidance_dirs, spec.guidance_dir)
    knowledge: dict[str, dict] = dict(spec.knowledge)
    blacklist: list[dict] = list(spec.blacklist)
    secrets: list[Secret] = list(spec.secrets)
    integrations: list[str] = list(spec.integrations)
    activations: list[ScenarioActivation] = list(spec.scenarios)

    for layer in _collect_layers(
        entry,
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    ):
        if layer.contacts:
            contacts = _merge_by_key(contacts, layer.contacts, _contact_key)
        if layer.guidance_dir is not None:
            guidance_dirs = _append_guidance_dir(guidance_dirs, layer.guidance_dir)
        if layer.knowledge:
            knowledge = _merge_knowledge(knowledge, layer.knowledge)
        if layer.blacklist:
            blacklist = _merge_by_key(blacklist, layer.blacklist, _blacklist_key)
        if layer.secrets:
            secrets = _merge_by_key(secrets, list(layer.secrets), _secret_key)
        if layer.integrations:
            integrations = _merge_integrations(integrations, list(layer.integrations))
        if layer.scenarios:
            activations = [*activations, *layer.scenarios]

    materialised_scenarios: list[Any] = []
    if activations:
        deployment_name = resolve_deployment_name(
            entry.mapping,
            user_id=user_id,
            org_id=org_id,
            team_ids=team_ids,
            assistant_id=assistant_id,
        )
        materialised_scenarios, activation_secrets = materialise_scenario_activations(
            activations,
            client_slug=client_name,
            deployment_name=deployment_name,
        )
        if activation_secrets:
            secrets = _merge_by_key(secrets, activation_secrets, _secret_key)

    file_secrets = load_secrets(
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    )
    if file_secrets:
        file_secret_models = [Secret(**s) for s in file_secrets]
        secrets = _merge_by_key(secrets, file_secret_models, _secret_key)

    return ResolvedAssistantDeployment(
        config=spec.actor_config,
        environments=list(spec.environments),
        function_dirs=[spec.function_dir] if spec.function_dir else [],
        venv_dirs=[spec.venv_dir] if spec.venv_dir else [],
        guidance_dirs=guidance_dirs,
        contacts=contacts,
        knowledge=knowledge,
        blacklist=blacklist,
        secrets=secrets,
        integrations=integrations,
        mcp_configs=[],
        url_mappings={},
        console_config=spec.console_config,
        scenarios=list(materialised_scenarios),
    )


def resolve_from_deployments(
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> ResolvedAssistantDeployment | None:
    """Walk ``_CLIENT_DEPLOYMENTS`` and return an isolated result if a match is found.

    Each registered client is checked in order:

    1. **Environment guardrail** — skip if registered for a different
       environment than what :func:`detect_environment` reports.
    2. **Mapping resolution** — walk the client's
       :class:`DeploymentMapping` to find the first matching target.
       If no target matches, move on to the next client.
    3. Return the spec converted to :class:`ResolvedAssistantDeployment`
       via :func:`_spec_to_resolved`.

    Returns ``None`` when no client matches.
    """
    from unity_deploy.assistant_deployments.deployment_types import (
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
            client_name=client_name,
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
) -> ResolvedAssistantDeployment:
    """Resolve the assistant deployment for the given identity.

    Walks ``_CLIENT_DEPLOYMENTS`` looking for a client whose mapping
    targets match the session identity.  Returns the deployment spec
    as a :class:`ResolvedAssistantDeployment`.

    When no client matches, returns an empty default assistant_deployments.
    """
    result = resolve_from_deployments(
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    )
    if result is not None:
        return result

    return ResolvedAssistantDeployment(
        config=ActorConfig(),
        environments=[],
        function_dirs=[],
        venv_dirs=[],
        guidance_dirs=[],
        contacts=[],
        knowledge={},
        blacklist=[],
        secrets=[],
        integrations=[],
        mcp_configs=[],
        url_mappings={},
        scenarios=[],
    )


# ---------------------------------------------------------------------------
# Import client subpackages so they self-register.
# Add new clients here.
# ---------------------------------------------------------------------------

# Specific-scope clients should register before broader org-level clients
# because resolution is first-match-wins in insertion order.
from . import client_alpha  # noqa: F401, E402  (specific assistant ids)
from . import clientepsilon_homes  # noqa: F401, E402  (specific assistant ids)
from . import clientzeta  # noqa: F401, E402  (specific assistant ids)
from . import client_beta  # noqa: F401, E402  (specific assistant/org ids)
from . import unify_company  # noqa: F401, E402  (Unify org + brain operator)

# TODO: Yasser has left the team.  Re-enable when a new ClientGamma deployment
# owner is assigned and _ENVIRONMENTS is populated in clientgamma/__init__.py.
# from . import clientgamma  # noqa: F401, E402
