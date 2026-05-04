"""Shared deployment types and utilities for client customization.

Every client under ``clients/`` can organise its logic into versioned
deployments, each exposing ``get_deployment() -> DeploymentSpec``.
Identity-based routing via :class:`DeploymentMapping` determines which
deployment loads for a given user / org / team / assistant.

Lightweight clients (config + secrets only) leave the data-pipeline
fields as ``None``; data-heavy clients populate ``pipeline_config``,
``function_dir``, and ``data_dir``.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from types import ModuleType

from unity_deploy.customization.configs.types.actor_config import ActorConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------


def detect_environment() -> str:
    """Detect the deployment environment from ``ORCHESTRA_URL``.

    Returns ``"staging"`` when the URL contains ``"staging"``,
    ``"development"`` when it points to localhost / 127.0.0.1,
    and ``"production"`` otherwise (including when unset, which
    defaults to ``https://api.unify.ai``).

    ``ORCHESTRA_URL`` is the canonical indicator of which Unify backend
    the application is connected to.  Known patterns::

        Production:  https://api.unify.ai/v0
        Staging:     https://internal.example.com/...
        Development: http://localhost:8000
    """
    url = os.environ.get("ORCHESTRA_URL", "").lower()
    if "staging" in url:
        return "staging"
    if "localhost" in url or "127.0.0.1" in url:
        return "development"
    return "production"


# ---------------------------------------------------------------------------
# Core deployment models
# ---------------------------------------------------------------------------


from unity.guidance_manager.types.guidance import Guidance
from unity.secret_manager.types import Secret
from unity_deploy.customization.scenarios.types import ScenarioActivation

GuidanceEntry = Guidance
SecretEntry = Secret


class SeedLayer(BaseModel):
    """Additive seed data registered at a specific identity scope.

    Layers are collected during resolution in scope order
    (org -> team -> user -> assistant) and merged onto the
    deployment spec's seed data.  More specific layers override
    less specific ones using natural-key dedup (contacts by
    name, guidance by title, knowledge tables by seed_key, etc.).
    """

    contacts: list[dict] = Field(default_factory=list)
    guidance: list[Guidance] = Field(default_factory=list)
    knowledge: dict[str, dict] = Field(default_factory=dict)
    blacklist: list[dict] = Field(default_factory=list)
    secrets: list[Secret] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    scenarios: list[ScenarioActivation] = Field(
        default_factory=list,
        description=(
            "Scenario activations for this layer.  Each activation names "
            "a generic template from a platform integration package and "
            "supplies the client-specific overrides needed to materialise "
            "a concrete scenario.  Empty list (default) means no per-layer "
            "scenarios; backwards compatible with all existing layers."
        ),
    )


def _merge_actor_configs(base: ActorConfig, override: ActorConfig) -> ActorConfig:
    """Deep-merge two :class:`ActorConfig` instances (base + override).

    For each field, the override value wins when non-None; otherwise the
    base value is kept.
    """
    merged: dict = {}
    for field_name in ActorConfig.model_fields:
        base_val = getattr(base, field_name)
        override_val = getattr(override, field_name)
        if override_val is not None:
            merged[field_name] = override_val
        elif base_val is not None:
            merged[field_name] = base_val
    return ActorConfig(**merged)


class DeploymentSpec(BaseModel):
    """Fully validated, self-contained deployment descriptor.

    Returned by each deployment's ``get_deployment()`` function.
    Everything the registration system needs is in this object.

    Fields ``pipeline_config``, ``function_dir``, and ``data_dir`` are
    optional -- lightweight deployments (e.g. config + secrets only)
    leave them as ``None``.

    Use :meth:`derive` to create a child deployment that inherits
    fields from this spec and overrides only what changed.
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str = Field(
        ...,
        description="Unique deployment identifier, e.g. 'v0'.",
    )
    actor_config: ActorConfig = Field(
        ...,
        description="Actor identity and capabilities config.",
    )
    guidance: list[Guidance] = Field(
        default_factory=list,
        description="Guidance entries registered with the actor.",
    )
    secrets: list[Secret] = Field(
        default_factory=list,
        description="Credentials registered with the actor's SecretManager.",
    )
    integrations: list[str] = Field(
        default_factory=list,
        description=(
            "Private integration package slugs enabled for this deployment. "
            "Loaded from unity_deploy.customization.integrations.packages."
        ),
    )
    scenarios: list[ScenarioActivation] = Field(
        default_factory=list,
        description=(
            "Static scenario activations bound to this deployment regardless "
            "of layer overlays.  Each activation names a generic scenario "
            "template from a platform integration package and supplies the "
            "client-specific overrides (assistant_id, scenario_id, etc.) "
            "needed to materialise a concrete scenario.  Use SeedLayer."
            "scenarios for activations that should only apply to specific "
            "scopes via register_layer overlays."
        ),
    )
    contacts: list[dict] = Field(
        default_factory=list,
        description="Contact records synced to the ContactManager.",
    )
    knowledge: dict[str, dict] = Field(
        default_factory=dict,
        description="Knowledge table specs synced to the KnowledgeManager.",
    )
    blacklist: list[dict] = Field(
        default_factory=list,
        description="Blacklist entries synced to the ContactManager.",
    )
    environments: list = Field(
        default_factory=list,
        description="BaseEnvironment instances forwarded to the CodeActActor.",
    )

    pipeline_config: Optional["PipelineConfig"] = Field(
        default=None,
        description="Validated pipeline config for this deployment (None for non-data deployments).",
    )
    function_dir: Optional[Path] = Field(
        default=None,
        description="Absolute path to the functions/ directory (None if no custom functions).",
    )
    venv_dir: Optional[Path] = Field(
        default=None,
        description="Absolute path to a custom venv directory (None if no custom venvs).",
    )
    data_dir: Optional[Path] = Field(
        default=None,
        description="Absolute path to the data/ directory with raw assets (None if no local data).",
    )
    console_config: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Per-assistant console UI configuration (layout mode, tab "
            "visibility, theme overrides). Reconciled to Orchestra at deploy "
            "time via unity_deploy.scripts.reconcile_control_plane, with the "
            "startup hook retaining a best-effort drift-repair PATCH. "
            "Orchestra stores it in the assistant_console_config table."
        ),
    )

    def derive(self, **overrides) -> "DeploymentSpec":
        """Create a derived deployment by overriding specific fields.

        ``actor_config`` is deep-merged: non-None fields in *overrides*
        win, while unset fields are inherited from this (base) spec.
        All other fields are replaced wholesale when provided.  Fields
        not passed are inherited unchanged.

        Example::

            BASE = DeploymentSpec(name="base", actor_config=ActorConfig(...))
            v2 = BASE.derive(name="v2", function_dir=Path("/new/funcs"))
        """
        if "actor_config" in overrides:
            overrides["actor_config"] = _merge_actor_configs(
                self.actor_config,
                overrides["actor_config"],
            )
        return self.model_copy(update=overrides)


# ---------------------------------------------------------------------------
# Deployment mapping / routing
# ---------------------------------------------------------------------------


class DeploymentTarget(BaseModel):
    """Maps an identity scope to a deployment name."""

    scope: Literal["user", "org", "team", "assistant", "default"] = Field(
        ...,
        description="Identity scope this target matches against.",
    )
    scope_id: Optional[str] = Field(
        None,
        description=(
            "The identifier within the scope (user_id, org_id string, etc.). "
            "None when scope='default'."
        ),
    )
    deployment: str = Field(
        ...,
        description="Deployment folder name under deployments/, e.g. 'v1'.",
    )


class DeploymentMapping(BaseModel):
    """Ordered list of identity-to-deployment bindings.

    Resolution walks the list top-to-bottom; the first matching target wins.
    A ``scope='default'`` entry should be last as a catch-all.
    """

    targets: list[DeploymentTarget] = Field(
        ...,
        description="Ordered list of mappings; first match wins.",
    )


class EnvironmentConfig(BaseModel):
    """Per-environment deployment routing for a client.

    Scoping (org-wide, user-wide, assistant-specific) is expressed
    entirely through :class:`DeploymentTarget` entries in the mapping.
    """

    mapping: DeploymentMapping = Field(
        ...,
        description="Identity-to-deployment routing for this environment.",
    )


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def resolve_deployment_name(
    mapping: DeploymentMapping,
    *,
    user_id: str | None = None,
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    assistant_id: int | None = None,
) -> str:
    """Walk *mapping* top-to-bottom and return the first matching deployment name.

    Raises :class:`ValueError` if no target matches (should not happen if a
    ``default`` entry exists).
    """
    for target in mapping.targets:
        if target.scope == "user" and target.scope_id == user_id:
            return target.deployment
        if target.scope == "org" and str(org_id) == target.scope_id:
            return target.deployment
        if target.scope == "team" and team_ids:
            for tid in team_ids:
                if str(tid) == target.scope_id:
                    return target.deployment
        if target.scope == "assistant" and str(assistant_id) == target.scope_id:
            return target.deployment
        if target.scope == "default":
            return target.deployment
    raise ValueError("No matching deployment target found in mapping")


def load_deployment(deployments_dir: Path, name: str) -> DeploymentSpec:
    """Dynamically import ``deployments_dir/{name}/deployment.py`` and return its spec.

    After loading, structural validation is performed via
    :func:`validate_deployment_dir`.

    Raises
    ------
    FileNotFoundError
        If the deployment directory or ``deployment.py`` does not exist,
        or declared capabilities don't match the directory contents.
    AttributeError
        If the module does not expose ``get_deployment()``.
    TypeError
        If ``get_deployment()`` does not return a :class:`DeploymentSpec`.
    """
    deployment_path = deployments_dir / name / "deployment.py"
    if not deployment_path.exists():
        raise FileNotFoundError(
            f"Deployment '{name}' not found at {deployment_path}",
        )

    spec = importlib.util.spec_from_file_location(
        f"deploy.{name}",
        deployment_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {deployment_path}")

    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    get_fn = getattr(module, "get_deployment", None)
    if get_fn is None:
        raise AttributeError(
            f"Deployment '{name}' is missing the required get_deployment() function",
        )

    result = get_fn()
    if not isinstance(result, DeploymentSpec):
        raise TypeError(
            f"get_deployment() must return DeploymentSpec, got {type(result).__name__}",
        )

    errors = validate_deployment_dir(result, deployments_dir / name)
    if errors:
        raise FileNotFoundError(
            f"Deployment '{name}' has structural issues:\n"
            + "\n".join(f"  - {e}" for e in errors),
        )

    return result


def validate_deployment_dir(
    spec: DeploymentSpec,
    deployment_dir: Path,
) -> list[str]:
    """Check that a deployment directory matches the capabilities declared in *spec*.

    Returns a list of error strings (empty means valid).
    """
    errors: list[str] = []

    if spec.function_dir is not None:
        if not spec.function_dir.is_dir():
            errors.append(f"function_dir does not exist: {spec.function_dir}")
        else:
            for required in ("helpers.py", "metrics.py"):
                if not (spec.function_dir / required).is_file():
                    errors.append(f"Missing {required} in {spec.function_dir}")

    if spec.data_dir is not None and not spec.data_dir.is_dir():
        errors.append(f"data_dir does not exist: {spec.data_dir}")

    if spec.pipeline_config is not None and spec.data_dir is not None:
        config_file = spec.data_dir / "pipeline_config.json"
        if not config_file.is_file():
            errors.append(f"pipeline_config declared but missing: {config_file}")

    return errors


def register_client(
    client_name: str,
    mapping: DeploymentMapping,
    deployments_dir: Path,
    *,
    environment: str | None = None,
) -> dict[str, DeploymentSpec]:
    """Register a client's deployment mapping for isolated resolution.

    Loads every deployment referenced in *mapping* and stores the
    mapping + loaded specs in the ``_CLIENT_DEPLOYMENTS`` registry
    inside ``clients/__init__``.  Resolution is handled by
    :func:`~unity_deploy.customization.clients.resolve_from_deployments`
    which returns the matching spec directly.

    Scoping (org-wide, user-wide, assistant-specific) is expressed
    entirely through :class:`DeploymentTarget` entries in *mapping*.

    Parameters
    ----------
    client_name
        Unique key for this client (e.g. ``"client_alpha"``).
    mapping
        The :class:`DeploymentMapping` for the current environment.
    deployments_dir
        Filesystem path containing ``<name>/deployment.py`` packages.
    environment
        The deployment environment this registration applies to
        (e.g. ``"staging"``, ``"production"``).  Stored as a guardrail:
        ``resolve_from_deployments`` verifies the runtime environment
        matches before returning a result.

    Returns
    -------
    dict[str, DeploymentSpec]
        Loaded specs keyed by deployment name.
    """
    from unity_deploy.customization.clients import (
        ClientDeploymentEntry,
        _CLIENT_DEPLOYMENTS,
    )

    loaded: dict[str, DeploymentSpec] = {}
    for target in mapping.targets:
        if target.deployment not in loaded:
            loaded[target.deployment] = load_deployment(
                deployments_dir,
                target.deployment,
            )

    _CLIENT_DEPLOYMENTS[client_name] = ClientDeploymentEntry(
        mapping=mapping,
        specs=loaded,
        environment=environment,
    )
    logger.info(
        "Registered client '%s' (%d deployment(s), env=%s)",
        client_name,
        len(loaded),
        environment,
    )
    return loaded


def register_layer(
    client_name: str,
    scope: str,
    scope_id: str,
    layer: SeedLayer,
) -> None:
    """Register shared seed data at a specific identity scope.

    Parameters
    ----------
    client_name
        Must match a previously registered client name.
    scope
        One of ``"org"``, ``"team"``, ``"user"``, ``"assistant"``.
    scope_id
        The identifier within the scope (e.g. org ID as a string,
        user UUID, assistant ID as a string).
    layer
        A :class:`SeedLayer` containing the additive seed data.

    Raises
    ------
    KeyError
        If *client_name* has not been registered via :func:`register_client`.
    ValueError
        If *scope* is not one of the allowed values.
    """
    from unity_deploy.customization.clients import _CLIENT_DEPLOYMENTS

    if client_name not in _CLIENT_DEPLOYMENTS:
        raise KeyError(
            f"Client '{client_name}' not registered. "
            f"Call register_client() before register_layer().",
        )
    if scope not in ("org", "team", "user", "assistant"):
        raise ValueError(
            f"Invalid scope '{scope}'. Must be one of: org, team, user, assistant.",
        )

    entry = _CLIENT_DEPLOYMENTS[client_name]
    key = f"{scope}:{scope_id}"
    entry.layers[key] = layer
    logger.debug(
        "Registered seed layer for client '%s' at %s",
        client_name,
        key,
    )


# Resolve the PipelineConfig forward reference in DeploymentSpec
from unity_deploy.customization.types.pipeline_config import (
    PipelineConfig,
)  # noqa: E402

DeploymentSpec.model_rebuild()
