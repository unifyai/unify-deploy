"""Routing manifest resolution for bundled client deployments."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from unify_deploy.assistant_deployments.deployment_types import detect_environment

_MANIFEST_PATH = Path(__file__).resolve().parent / "routing_manifest.yaml"


@dataclass(frozen=True)
class BundleTarget:
    """Resolved client bundle identity for a session."""

    client_name: str
    deployment: str
    bundle_key: str


@dataclass(frozen=True)
class ManifestLayer:
    scope: str
    scope_id: str
    integrations: tuple[str, ...] = ()
    contacts_dir: str | None = None
    knowledge_dir: str | None = None
    custom_data_dir: str | None = None
    dashboards_dir: str | None = None
    tasks_dir: str | None = None
    files_dir: str | None = None
    guidance_dir: str | None = None
    secrets_dir: str | None = None
    blacklist_dir: str | None = None


def _normalize_scope_id(scope: str, scope_id: str | int) -> str:
    return str(scope_id)


def _target_matches(
    target: dict[str, Any],
    *,
    org_id: int | None,
    team_ids: list[int] | None,
    user_id: str | None,
    assistant_id: int | None,
) -> bool:
    scope = target["scope"]
    scope_id = _normalize_scope_id(scope, target["scope_id"])
    if scope == "assistant":
        return assistant_id is not None and str(assistant_id) == scope_id
    if scope == "user":
        return user_id is not None and user_id == scope_id
    if scope == "org":
        return org_id is not None and str(org_id) == scope_id
    if scope == "team":
        return team_ids is not None and int(scope_id) in team_ids
    return False


@lru_cache(maxsize=1)
def load_routing_manifest() -> dict[str, Any]:
    with _MANIFEST_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def resolve_client_bundle_target(
    *,
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> BundleTarget | None:
    """Return the bundled client target for the given session identity."""

    manifest = load_routing_manifest()
    environment = detect_environment()
    for client_name, client_cfg in manifest.get("clients", {}).items():
        env_cfg = client_cfg.get("environments", {}).get(environment, {})
        for target in env_cfg.get("targets", []):
            if _target_matches(
                target,
                org_id=org_id,
                team_ids=team_ids,
                user_id=user_id,
                assistant_id=assistant_id,
            ):
                return BundleTarget(
                    client_name=client_name,
                    deployment=target["deployment"],
                    bundle_key=client_cfg.get("bundle_key", client_name),
                )
    return None


def resolve_manifest_layers(
    client_name: str,
    *,
    org_id: int | None = None,
    team_ids: list[int] | None = None,
    user_id: str | None = None,
    assistant_id: int | None = None,
) -> list[ManifestLayer]:
    """Return seed layers declared in the manifest for *client_name*."""

    manifest = load_routing_manifest()
    client_cfg = manifest.get("clients", {}).get(client_name, {})
    environment = detect_environment()
    env_cfg = client_cfg.get("environments", {}).get(environment, {})
    matched: list[ManifestLayer] = []
    for layer in env_cfg.get("layers", []):
        if _target_matches(
            layer,
            org_id=org_id,
            team_ids=team_ids,
            user_id=user_id,
            assistant_id=assistant_id,
        ):
            matched.append(
                ManifestLayer(
                    scope=layer["scope"],
                    scope_id=_normalize_scope_id(layer["scope"], layer["scope_id"]),
                    integrations=tuple(layer.get("integrations", [])),
                    contacts_dir=layer.get("contacts_dir"),
                    knowledge_dir=layer.get("knowledge_dir"),
                    custom_data_dir=layer.get("custom_data_dir"),
                    dashboards_dir=layer.get("dashboards_dir"),
                    tasks_dir=layer.get("tasks_dir"),
                    files_dir=layer.get("files_dir"),
                    guidance_dir=layer.get("guidance_dir"),
                    secrets_dir=layer.get("secrets_dir"),
                    blacklist_dir=layer.get("blacklist_dir"),
                ),
            )
    return matched
