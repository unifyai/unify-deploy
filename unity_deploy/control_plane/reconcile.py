"""Deploy-time reconciliation for client control-plane state.

The Unity startup hook is wake-time runtime hydration.  This module handles
durable metadata that Console and Orchestra need before any assistant wakes,
starting with assistant-scoped ``console_config``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from unity_deploy.customization.clients import ClientDeploymentEntry

_ASSISTANT_UPDATE_PATH = "/admin/assistant/{assistant_id}"


@dataclass(frozen=True)
class ReconcileOperation:
    """One Orchestra admin update needed to match deployment desired state."""

    client_name: str
    assistant_id: str
    deployment: str
    field: str
    action: str
    path: str
    payload: dict[str, Any]


def _load_registry() -> Mapping[str, "ClientDeploymentEntry"]:
    # Importing the clients package self-registers environment-active clients.
    from unity_deploy.customization.clients import _CLIENT_DEPLOYMENTS

    return _CLIENT_DEPLOYMENTS


def build_control_plane_plan(
    *,
    environment: str,
    client: str | None = None,
    assistant_id: str | int | None = None,
    registry: Mapping[str, "ClientDeploymentEntry"] | None = None,
) -> list[ReconcileOperation]:
    """Return desired Orchestra writes for assistant-scoped deployment state."""
    entries = registry if registry is not None else _load_registry()
    assistant_filter = str(assistant_id) if assistant_id is not None else None
    operations: list[ReconcileOperation] = []

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

            spec = entry.specs[target.deployment]
            console_config = spec.console_config
            operations.append(
                ReconcileOperation(
                    client_name=client_name,
                    assistant_id=target_assistant_id,
                    deployment=target.deployment,
                    field="console_config",
                    action="upsert" if console_config is not None else "clear",
                    path=_ASSISTANT_UPDATE_PATH.format(
                        assistant_id=target_assistant_id,
                    ),
                    payload={"console_config": console_config},
                ),
            )

    return operations


def apply_operations(operations: list[ReconcileOperation]) -> list[dict[str, Any]]:
    """Apply planned operations to Orchestra and return parsed responses."""
    from unity_deploy.utils.orchestra_client import patch_json

    responses: list[dict[str, Any]] = []
    for operation in operations:
        responses.append(patch_json(operation.path, operation.payload))
    return responses


def format_operation(operation: ReconcileOperation) -> str:
    """Human-readable line for dry-run/apply logs."""
    return (
        f"{operation.action} {operation.field} "
        f"client={operation.client_name} "
        f"assistant_id={operation.assistant_id} "
        f"deployment={operation.deployment} "
        f"path={operation.path}"
    )
