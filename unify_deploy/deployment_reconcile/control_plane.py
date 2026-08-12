"""Control-plane plane for deploy-time reconciliation.

This module handles
durable metadata that the control plane needs before any assistant wakes,
including Orchestra assistant metadata and Communication task activations.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from typing import Any, Mapping, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from unify_deploy.assistant_deployments.clients import ClientDeploymentEntry

_ASSISTANT_UPDATE_PATH = "/admin/assistant/{assistant_id}"


@dataclass(frozen=True)
class ReconcileOperation:
    """One control-plane write needed to match deployment desired state."""

    client_name: str
    assistant_id: str
    deployment: str
    field: str
    action: str
    path: str
    payload: dict[str, Any]
    service: str = "orchestra"
    method: str = "patch"
    missing_ok: bool = False


def _load_registry(
    *,
    environment: str | None = None,
) -> Mapping[str, "ClientDeploymentEntry"]:
    from unify_deploy.deployment_reconcile.registry import load_deployment_registry

    return load_deployment_registry(environment=environment)


def build_control_plane_plan(
    *,
    environment: str,
    client: str | None = None,
    assistant_id: str | int | None = None,
    registry: Mapping[str, "ClientDeploymentEntry"] | None = None,
) -> list[ReconcileOperation]:
    """Return desired writes for assistant-scoped deployment state."""
    entries = (
        registry if registry is not None else _load_registry(environment=environment)
    )
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
                    missing_ok=target.missing_ok,
                ),
            )
            # A required target is one this client cannot run without, so
            # Orchestra refuses to delete it. Deriving the flag from
            # ``missing_ok`` on every reconcile keeps the protected set equal
            # to what the deployments actually declare, and demoting a target
            # to optional releases it again.
            required = not target.missing_ok
            operations.append(
                ReconcileOperation(
                    client_name=client_name,
                    assistant_id=target_assistant_id,
                    deployment=target.deployment,
                    field="deployment_target",
                    action="upsert" if required else "clear",
                    path=_ASSISTANT_UPDATE_PATH.format(
                        assistant_id=target_assistant_id,
                    ),
                    payload={"is_deployment_target": required},
                    missing_ok=target.missing_ok,
                ),
            )

    return operations


def apply_operations(operations: list[ReconcileOperation]) -> list[dict[str, Any]]:
    """Apply planned operations to Orchestra and return parsed responses."""
    import httpx

    from unify_deploy.utils.orchestra_client import OrchestraClientError, patch_json

    responses: list[dict[str, Any]] = []
    missing_optional_assistants: set[str] = set()

    def skip_missing(operation: ReconcileOperation, reason: str) -> None:
        logger.warning(
            "Skipping optional control-plane %s for assistant %s: %s",
            operation.field,
            operation.assistant_id,
            reason,
        )
        responses.append(
            {
                "status": "skipped-missing",
                "assistant_id": operation.assistant_id,
                "field": operation.field,
                "reason": reason,
            },
        )

    for operation in operations:
        if (
            operation.missing_ok
            and operation.assistant_id in missing_optional_assistants
        ):
            skip_missing(operation, "assistant target is missing")
            continue
        if operation.action == "deferred":
            reason = operation.payload.get("deferred_reason", operation.field)
            logger.info(
                "Deferring control-plane %s for assistant %s to the runtime "
                "plane: %s",
                operation.field,
                operation.assistant_id,
                reason,
            )
            responses.append(
                {
                    "status": "deferred",
                    "assistant_id": operation.assistant_id,
                    "field": operation.field,
                    "reason": reason,
                },
            )
            continue
        if operation.action == "unresolved":
            raise RuntimeError(
                "Cannot apply unresolved control-plane operation: "
                f"{operation.payload.get('unresolved_reason', operation.field)}",
            )
        try:
            if operation.service == "communication":
                responses.append(
                    _post_communication_json(operation.path, operation.payload),
                )
            else:
                responses.append(patch_json(operation.path, operation.payload))
        except (OrchestraClientError, httpx.HTTPStatusError) as exc:
            status_code = (
                exc.status_code
                if isinstance(exc, OrchestraClientError)
                else exc.response.status_code
            )
            if status_code != 404:
                raise
            if not operation.missing_ok:
                raise RuntimeError(_missing_required_target(operation)) from exc
            reason = f"assistant target {operation.assistant_id} not found"
            missing_optional_assistants.add(operation.assistant_id)
            skip_missing(operation, reason)
    return responses


def _missing_required_target(operation: ReconcileOperation) -> str:
    """Explain a vanished required target in terms of the edit that fixes it.

    A required target is one its client declares it cannot run without, so a
    missing assistant row stops the deploy. The id is recorded in several
    places that have to agree, and naming them here is the difference between
    a bare 404 and a message someone can act on.
    """
    return (
        f"Deployment target assistant {operation.assistant_id} does not exist "
        f"in Orchestra (client {operation.client_name!r}, deployment "
        f"{operation.deployment!r}, field {operation.field!r}). This target is "
        "required, so reconciliation stops rather than deploying a client with "
        "no assistant behind it. Either recreate the assistant, or repoint the "
        "client at a live one by updating its id everywhere the deployment "
        "records it: the client's own operator-id mapping, "
        "unify_deploy/assistant_deployments/routing_manifest.yaml, and this "
        "environment's Cloud Build assistant-id substitution."
    )


def _post_communication_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST an admin-authenticated JSON payload to Communication."""

    import httpx
    from unify.settings import SETTINGS

    comms_url = (
        os.environ.get("UNITY_COMMS_URL")
        or os.environ.get("COMMUNICATION_URL")
        or os.environ.get("COMMS_URL")
        or getattr(SETTINGS.conversation, "COMMS_URL", "")
    ).rstrip("/")
    if not comms_url:
        raise RuntimeError(
            "UNITY_COMMS_URL, COMMUNICATION_URL, or COMMS_URL is required "
            "for communication control-plane operations",
        )
    admin_key = SETTINGS.ORCHESTRA_ADMIN_KEY.get_secret_value()
    if not admin_key:
        raise RuntimeError("ORCHESTRA_ADMIN_KEY is required for Communication writes")

    normalized_path = path if path.startswith("/") else f"/{path}"
    response = httpx.post(
        f"{comms_url}{normalized_path}",
        json=payload,
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=30.0,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected Communication response for {path}")
    return body


def format_operation(operation: ReconcileOperation) -> str:
    """Human-readable line for dry-run/apply logs."""
    return (
        f"{operation.action} {operation.field} "
        f"service={operation.service} "
        f"client={operation.client_name} "
        f"assistant_id={operation.assistant_id} "
        f"deployment={operation.deployment} "
        f"path={operation.path}"
    )
