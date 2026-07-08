"""Load assistant deployment specs from unpacked client bundles."""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from functools import lru_cache
from pathlib import Path

from unify.secret_manager.types import Secret
from unity_deploy.assistant_deployments.clients import ResolvedAssistantDeployment
from unity_deploy.assistant_deployments.deployment_types import DeploymentSpec
from unity_deploy.assistant_deployments.routing_manifest import (
    ManifestLayer,
    resolve_manifest_layers,
)
from unity_deploy.client_bundle.fetch import client_deployment_root

logger = logging.getLogger(__name__)


def _register_bundle_package(client_name: str, root: Path) -> None:
    """Expose the unpacked bundle under the canonical client import path."""

    client_pkg = f"unity_deploy.assistant_deployments.clients.{client_name}"
    if client_pkg not in sys.modules:
        module = types.ModuleType(client_pkg)
        init_path = root / "__init__.py"
        module.__file__ = str(init_path if init_path.exists() else root)
        module.__path__ = [str(root)]
        sys.modules[client_pkg] = module


def _load_get_deployment(deployment_dir: Path, *, client_name: str):
    root = deployment_dir.parent.parent
    _register_bundle_package(client_name, root)
    module_path = deployment_dir / "deployment.py"
    spec = importlib.util.spec_from_file_location(
        f"client_bundle.{deployment_dir.name}.deployment",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load deployment module at {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    getter = getattr(module, "get_deployment", None)
    if getter is None:
        raise AttributeError(f"Missing get_deployment() in {module_path}")
    return getter


@lru_cache(maxsize=32)
def _cached_deployment_spec(
    client_root: str,
    deployment: str,
    client_name: str,
) -> DeploymentSpec:
    root = Path(client_root)
    deployment_dir = root / "deployments" / deployment
    getter = _load_get_deployment(deployment_dir, client_name=client_name)
    spec = getter()
    if not isinstance(spec, DeploymentSpec):
        raise TypeError(
            f"get_deployment() in {deployment_dir} did not return DeploymentSpec",
        )
    return spec


def _resolve_layer_path(client_root: Path, relative: str | None) -> Path | None:
    if not relative:
        return None
    path = client_root / relative
    return path if path.exists() else None


def _apply_manifest_layers(
    resolved: ResolvedAssistantDeployment,
    *,
    client_root: Path,
    client_name: str,
    org_id: int | None,
    team_ids: list[int] | None,
    user_id: str | None,
    assistant_id: int | None,
) -> ResolvedAssistantDeployment:
    layers: list[ManifestLayer] = resolve_manifest_layers(
        client_name,
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    )
    if not layers:
        return resolved

    contacts_dirs = list(resolved.contacts_dirs)
    guidance_dirs = list(resolved.guidance_dirs)
    knowledge_dirs = list(resolved.knowledge_dirs)
    custom_data_dirs = list(resolved.custom_data_dirs)
    secrets_dirs = list(resolved.secrets_dirs)
    blacklist_dirs = list(resolved.blacklist_dirs)
    integrations = list(resolved.integrations)

    for layer in layers:
        contacts = _resolve_layer_path(client_root, layer.contacts_dir)
        if contacts is not None:
            contacts_dirs.append(contacts)
        guidance = _resolve_layer_path(client_root, layer.guidance_dir)
        if guidance is not None:
            guidance_dirs.append(guidance)
        knowledge = _resolve_layer_path(client_root, layer.knowledge_dir)
        if knowledge is not None:
            knowledge_dirs.append(knowledge)
        custom_data = _resolve_layer_path(client_root, layer.custom_data_dir)
        if custom_data is not None:
            custom_data_dirs.append(custom_data)
        secrets = _resolve_layer_path(client_root, layer.secrets_dir)
        if secrets is not None:
            secrets_dirs.append(secrets)
        blacklist = _resolve_layer_path(client_root, layer.blacklist_dir)
        if blacklist is not None:
            blacklist_dirs.append(blacklist)
        integrations.extend(layer.integrations)

    return ResolvedAssistantDeployment(
        config=resolved.config,
        environments=resolved.environments,
        function_dirs=resolved.function_dirs,
        venv_dirs=resolved.venv_dirs,
        guidance_dirs=guidance_dirs,
        contacts_dirs=contacts_dirs,
        secrets_dirs=secrets_dirs,
        knowledge_dirs=knowledge_dirs,
        custom_data_dirs=custom_data_dirs,
        blacklist_dirs=blacklist_dirs,
        secrets=resolved.secrets,
        integrations=integrations,
        integration_registry=resolved.integration_registry,
        mcp_configs=resolved.mcp_configs,
        url_mappings=resolved.url_mappings,
        console_config=resolved.console_config,
        scenarios=resolved.scenarios,
    )


def _spec_to_resolved(
    spec: DeploymentSpec,
    *,
    client_name: str,
    deployment: str,
    org_id: int | None,
    team_ids: list[int] | None,
    user_id: str | None,
    assistant_id: int | None,
) -> ResolvedAssistantDeployment:
    from unity_deploy.assistant_deployments.scenarios.loader import (
        materialise_scenario_activations,
    )
    from unity_deploy.assistant_deployments.secrets_file import load_secrets

    function_dirs: list[Path] = []
    if spec.function_dir is not None:
        function_dirs.append(spec.function_dir)
    guidance_dirs: list[Path] = []
    if spec.guidance_dir is not None:
        guidance_dirs.append(spec.guidance_dir)
    contacts_dirs: list[Path] = []
    if spec.contacts_dir is not None:
        contacts_dirs.append(spec.contacts_dir)
    secrets_dirs: list[Path] = []
    if spec.secrets_dir is not None:
        secrets_dirs.append(spec.secrets_dir)
    knowledge_dirs: list[Path] = []
    if spec.knowledge_dir is not None:
        knowledge_dirs.append(spec.knowledge_dir)
    custom_data_dirs: list[Path] = []
    if spec.custom_data_dir is not None:
        custom_data_dirs.append(spec.custom_data_dir)
    blacklist_dirs: list[Path] = []
    if spec.blacklist_dir is not None:
        blacklist_dirs.append(spec.blacklist_dir)

    secrets: list[Secret] = list(spec.secrets or [])
    materialised_scenarios: list = []
    activations = list(spec.scenarios or [])
    if activations:
        materialised_scenarios, activation_secrets = materialise_scenario_activations(
            activations,
            client_slug=client_name,
            deployment_name=deployment,
        )
        if activation_secrets:
            secrets = [*secrets, *activation_secrets]

    file_secrets = load_secrets(
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    )
    if file_secrets:
        secrets = [*secrets, *[Secret(**entry) for entry in file_secrets]]

    return ResolvedAssistantDeployment(
        config=spec.actor_config,
        environments=list(spec.environments or []),
        function_dirs=function_dirs,
        venv_dirs=[spec.venv_dir] if spec.venv_dir else [],
        guidance_dirs=guidance_dirs,
        contacts_dirs=contacts_dirs,
        secrets_dirs=secrets_dirs,
        knowledge_dirs=knowledge_dirs,
        custom_data_dirs=custom_data_dirs,
        blacklist_dirs=blacklist_dirs,
        secrets=secrets,
        integrations=list(spec.integrations or []),
        mcp_configs=[],
        url_mappings={},
        console_config=spec.console_config,
        scenarios=list(materialised_scenarios),
    )


def resolve_from_bundle(
    *,
    client_name: str,
    deployment: str,
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> ResolvedAssistantDeployment | None:
    root = client_deployment_root()
    if root is None:
        logger.warning(
            "CLIENT_DEPLOYMENT_ROOT is unset; cannot resolve bundled deployment",
        )
        return None

    spec = _cached_deployment_spec(str(root.resolve()), deployment, client_name)
    resolved = _spec_to_resolved(
        spec,
        client_name=client_name,
        deployment=deployment,
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    )
    return _apply_manifest_layers(
        resolved,
        client_root=root,
        client_name=client_name,
        org_id=org_id,
        team_ids=team_ids,
        user_id=user_id,
        assistant_id=assistant_id,
    )
