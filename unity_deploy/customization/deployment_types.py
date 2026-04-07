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
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from types import ModuleType

from unity_deploy.customization.configs.types.actor_config import ActorConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core deployment models
# ---------------------------------------------------------------------------


class GuidanceEntry(BaseModel):
    """A single guidance entry registered with the actor."""

    title: str = Field(..., description="Short, descriptive title for the guidance.")
    content: str = Field(..., description="Full guidance text.")


class SecretEntry(BaseModel):
    """A single secret credential registered with the actor."""

    name: str = Field(..., description="Environment-style key, e.g. 'MS365_TENANT_ID'.")
    value: str = Field(..., description="The secret value.")
    description: str = Field(
        ...,
        description="Human-readable explanation of what this secret is and how to use it.",
    )


class DeploymentSpec(BaseModel):
    """Fully validated, self-contained deployment descriptor.

    Returned by each deployment's ``get_deployment()`` function.
    Everything the registration system needs is in this object.

    Fields ``pipeline_config``, ``function_dir``, and ``data_dir`` are
    optional -- lightweight deployments (e.g. config + secrets only)
    leave them as ``None``.
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
    guidance: list[GuidanceEntry] = Field(
        default_factory=list,
        description="Guidance entries registered with the actor.",
    )
    secrets: list[SecretEntry] = Field(
        default_factory=list,
        description="Credentials registered with the actor's SecretManager.",
    )

    pipeline_config: Optional["PipelineConfig"] = Field(
        default=None,
        description="Validated pipeline config for this deployment (None for non-data deployments).",
    )
    function_dir: Optional[Path] = Field(
        default=None,
        description="Absolute path to the functions/ directory (None if no custom functions).",
    )
    data_dir: Optional[Path] = Field(
        default=None,
        description="Absolute path to the data/ directory with raw assets (None if no local data).",
    )


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


def register_deployment(
    dep: DeploymentSpec,
    *,
    user_id: str | None = None,
    org_id: int | None = None,
    team_id: int | None = None,
    assistant_id: int | None = None,
) -> None:
    """Register a loaded deployment with the customization registry.

    Passes ``actor_config``, ``function_dir`` (if present), ``guidance``,
    and ``secrets`` (if present) to the appropriate ``register_*`` call.

    Priority (only one fires): assistant > user > team > org.
    """
    from unity_deploy.customization.clients import (
        register_assistant,
        register_org,
        register_team,
        register_user,
    )

    guidance_dicts = [entry.model_dump() for entry in dep.guidance]
    secrets_dicts = (
        [entry.model_dump() for entry in dep.secrets] if dep.secrets else None
    )

    kwargs: dict = {"config": dep.actor_config}
    if dep.function_dir is not None:
        kwargs["function_dir"] = dep.function_dir
    if guidance_dicts:
        kwargs["guidance"] = guidance_dicts
    if secrets_dicts:
        kwargs["secrets"] = secrets_dicts

    if assistant_id is not None:
        register_assistant(assistant_id, **kwargs)
        logger.info(
            "Registered deployment '%s' for assistant %s",
            dep.name,
            assistant_id,
        )
    elif user_id is not None:
        register_user(user_id, **kwargs)
        logger.info(
            "Registered deployment '%s' for user %s (%d guidance, %d secrets, "
            "functions: %s)",
            dep.name,
            user_id,
            len(dep.guidance),
            len(dep.secrets),
            dep.function_dir,
        )
    elif team_id is not None:
        register_team(team_id, **kwargs)
        logger.info(
            "Registered deployment '%s' for team %s",
            dep.name,
            team_id,
        )
    elif org_id is not None:
        register_org(org_id, **kwargs)
        logger.info(
            "Registered deployment '%s' for org %s",
            dep.name,
            org_id,
        )


def register_all_deployments(
    mapping: DeploymentMapping,
    deployments_dir: Path,
    *,
    default_org_id: int | None = None,
    default_user_id: str | None = None,
) -> dict[str, DeploymentSpec]:
    """Load and register every deployment referenced in *mapping*.

    Each :class:`DeploymentTarget` in the mapping is registered under
    its declared scope:

    - ``user``      → ``register_deployment(dep, user_id=scope_id)``
    - ``org``       → ``register_deployment(dep, org_id=int(scope_id))``
    - ``team``      → ``register_deployment(dep, team_id=int(scope_id))``
    - ``assistant`` → ``register_deployment(dep, assistant_id=int(scope_id))``
    - ``default``   → registered at org level (*default_org_id*) or
                      user level (*default_user_id*), whichever is given.

    This enables multiple deployments to be live simultaneously — e.g.
    v1 for a specific user while v0 serves the rest of the org.

    Returns a dict of ``deployment_name → DeploymentSpec`` for every
    unique deployment that was loaded.
    """
    loaded: dict[str, DeploymentSpec] = {}

    for target in mapping.targets:
        if target.deployment not in loaded:
            loaded[target.deployment] = load_deployment(
                deployments_dir,
                target.deployment,
            )

        dep = loaded[target.deployment]

        if target.scope == "user":
            register_deployment(dep, user_id=target.scope_id)

        elif target.scope == "org":
            register_deployment(dep, org_id=int(target.scope_id))  # type: ignore[arg-type]

        elif target.scope == "team":
            register_deployment(dep, team_id=int(target.scope_id))  # type: ignore[arg-type]

        elif target.scope == "assistant":
            register_deployment(dep, assistant_id=int(target.scope_id))  # type: ignore[arg-type]

        elif target.scope == "default":
            if default_org_id is not None:
                register_deployment(dep, org_id=default_org_id)
            elif default_user_id is not None:
                register_deployment(dep, user_id=default_user_id)
            else:
                logger.warning(
                    "Skipping 'default' target for deployment '%s' — "
                    "provide default_org_id or default_user_id to register it",
                    target.deployment,
                )

    return loaded


# Resolve the PipelineConfig forward reference in DeploymentSpec
from unity_deploy.customization.types.pipeline_config import (
    PipelineConfig,
)  # noqa: E402

DeploymentSpec.model_rebuild()
