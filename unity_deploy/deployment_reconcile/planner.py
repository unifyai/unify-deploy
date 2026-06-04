"""Planner for deploy-time reconciliation work."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from unity_deploy.deployment_reconcile.control_plane import build_control_plane_plan
from unity_deploy.runtime_reconcile import (
    compute_runtime_state_fingerprint,
)
from unity_deploy.deployment_reconcile.types import (
    DeploymentTargetPlan,
    DeploymentWorkItem,
    Plane,
)
from unity_deploy.startup_config import expand_startup_integrations


def _load_registry() -> Mapping[str, Any]:
    from unity_deploy.assistant_deployments.clients import _CLIENT_DEPLOYMENTS

    return _CLIENT_DEPLOYMENTS


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
    ).hexdigest()


def _runtime_summary(resolved: Any) -> dict[str, Any]:
    return {
        "contacts": len(resolved.contacts),
        "guidance": len(resolved.guidance),
        "knowledge_tables": sorted(resolved.knowledge.keys()),
        "blacklist": len(resolved.blacklist),
        "secrets": len(resolved.secrets),
        "integrations": list(resolved.integrations),
        "mcp_configs": len(resolved.mcp_configs),
        "function_dirs": [str(path) for path in resolved.function_dirs],
        "venv_dirs": [str(path) for path in resolved.venv_dirs],
    }


def _control_plane_revision(operations: Iterable[Any]) -> str:
    payload = [
        {
            "service": op.service,
            "method": op.method,
            "field": op.field,
            "action": op.action,
            "path": op.path,
            "payload": op.payload,
            "missing_ok": op.missing_ok,
        }
        for op in operations
    ]
    return _hash_payload(payload)


def _target_control_plane_operations(
    *,
    environment: str,
    client: str,
    assistant_id: str,
    registry: Mapping[str, Any],
) -> tuple[Any, ...]:
    return tuple(
        build_control_plane_plan(
            environment=environment,
            client=client,
            assistant_id=assistant_id,
            registry=registry,
        ),
    )


def build_deployment_target_plans(
    *,
    environment: str,
    client: str | None = None,
    assistant_id: str | int | None = None,
    registry: Mapping[str, Any] | None = None,
) -> list[DeploymentTargetPlan]:
    """Build deploy-time plans for explicit assistant-scoped targets."""

    from unity_deploy.assistant_deployments.clients import _spec_to_resolved

    entries = registry if registry is not None else _load_registry()
    assistant_filter = str(assistant_id) if assistant_id is not None else None
    plans: list[DeploymentTargetPlan] = []

    for client_name, entry in entries.items():
        if client is not None and client_name != client:
            continue
        if entry.environment is not None and entry.environment != environment:
            continue

        for target in entry.mapping.targets:
            if target.scope != "assistant" or target.scope_id is None:
                continue
            target_assistant_id = str(target.scope_id)
            if assistant_filter is not None and target_assistant_id != assistant_filter:
                continue

            assistant_id_int = (
                int(target_assistant_id) if target_assistant_id.isdigit() else None
            )
            spec = entry.specs[target.deployment]
            resolved = _spec_to_resolved(
                spec,
                entry,
                client_name=client_name,
                assistant_id=assistant_id_int,
            )
            resolved = expand_startup_integrations(resolved)
            runtime_revision = compute_runtime_state_fingerprint(resolved)
            control_ops = _target_control_plane_operations(
                environment=environment,
                client=client_name,
                assistant_id=target_assistant_id,
                registry=entries,
            )
            plans.append(
                DeploymentTargetPlan(
                    environment=environment,
                    client_name=client_name,
                    deployment=target.deployment,
                    assistant_id=target_assistant_id,
                    resolved=resolved,
                    missing_ok=target.missing_ok,
                    control_plane_operations=control_ops,
                    runtime_revision=runtime_revision,
                    control_plane_revision=_control_plane_revision(control_ops),
                    runtime_summary={
                        **_runtime_summary(resolved),
                        "target_key": f"assistant/{target_assistant_id}",
                    },
                ),
            )

    return plans


def build_deployment_work_items(
    *,
    environment: str,
    planes: Iterable[Plane] = ("control-plane",),
    client: str | None = None,
    assistant_id: str | int | None = None,
    registry: Mapping[str, Any] | None = None,
) -> list[DeploymentWorkItem]:
    """Build independently executable work items."""

    selected = set(planes)
    items: list[DeploymentWorkItem] = []
    for target in build_deployment_target_plans(
        environment=environment,
        client=client,
        assistant_id=assistant_id,
        registry=registry,
    ):
        if "control-plane" in selected:
            items.append(
                DeploymentWorkItem(
                    target=target,
                    plane="control-plane",
                    revision=target.control_plane_revision,
                    idempotency_key=f"{target.target_key}/control-plane",
                ),
            )
        if "runtime" in selected:
            items.append(
                DeploymentWorkItem(
                    target=target,
                    plane="runtime",
                    revision=target.runtime_revision,
                    idempotency_key=f"{target.target_key}/runtime",
                ),
            )
    return items
