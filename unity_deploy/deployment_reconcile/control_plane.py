"""Control-plane plane for deploy-time reconciliation.

This module handles
durable metadata that the control plane needs before any assistant wakes,
including Orchestra assistant metadata and Communication task activations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from typing import Any, Mapping, TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from unity_deploy.assistant_deployments.clients import ClientDeploymentEntry

_ASSISTANT_UPDATE_PATH = "/admin/assistant/{assistant_id}"
_TASK_ACTIVATION_UPSERT_PATH = "/infra/task-activation/upsert"


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


def _load_registry() -> Mapping[str, "ClientDeploymentEntry"]:
    # Importing the clients package self-registers environment-active clients.
    from unity_deploy.assistant_deployments.clients import _CLIENT_DEPLOYMENTS

    return _CLIENT_DEPLOYMENTS


def build_control_plane_plan(
    *,
    environment: str,
    client: str | None = None,
    assistant_id: str | int | None = None,
    registry: Mapping[str, "ClientDeploymentEntry"] | None = None,
) -> list[ReconcileOperation]:
    """Return desired writes for assistant-scoped deployment state."""
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
                    missing_ok=target.missing_ok,
                ),
            )
            operations.extend(
                _build_scenario_schedule_operations(
                    client_name=client_name,
                    entry=entry,
                    spec=spec,
                    assistant_id=target_assistant_id,
                    deployment=target.deployment,
                    missing_ok=target.missing_ok,
                ),
            )

    return operations


def _build_scenario_schedule_operations(
    *,
    client_name: str,
    entry: "ClientDeploymentEntry",
    spec: Any,
    assistant_id: str,
    deployment: str,
    missing_ok: bool,
) -> list[ReconcileOperation]:
    """Project private scenario schedules into generic task activation operations."""

    from unity_deploy.assistant_deployments.clients import _spec_to_resolved
    from unity_deploy.assistant_deployments.integrations.activation import (
        expand_integrations,
    )

    assistant_id_int = int(assistant_id) if str(assistant_id).isdigit() else None
    resolved = _spec_to_resolved(
        spec,
        entry,
        client_name=client_name,
        assistant_id=assistant_id_int,
    )
    resolved = expand_integrations(resolved)

    operations: list[ReconcileOperation] = []
    for scenario in resolved.scenarios:
        for task in scenario.tasks:
            if not task.enabled:
                continue
            if str(task.target.assistant_id) != assistant_id:
                continue
            operations.append(
                ReconcileOperation(
                    client_name=client_name,
                    assistant_id=assistant_id,
                    deployment=deployment,
                    field="task_activation",
                    action=_scenario_task_activation_action(task),
                    path=_TASK_ACTIVATION_UPSERT_PATH,
                    payload=_scenario_task_activation_payload(
                        client_name=client_name,
                        deployment=deployment,
                        scenario=scenario,
                        task=task,
                    ),
                    service="communication",
                    method="post",
                    missing_ok=missing_ok,
                ),
            )
    return operations


def _scenario_task_activation_action(task: Any) -> str:
    """Return whether a scenario-backed task can be materialized now.

    The control plane runs at deploy time, *before* the assistant wakes.  The
    authoritative seeder is the runtime plane
    (:func:`unity_deploy.runtime_reconcile.materialize.materialize_runtime_state`),
    which runs in the woken assistant's own identity/context and calls
    ``sync_all_seed_data`` (seeds the scenario's TaskScheduler tasks) plus
    ``FunctionManager.sync_custom`` (registers the entrypoint functions).  Until
    that has happened the activation ids do not exist yet, so a brand-new
    activation is ``"deferred"`` rather than a hard failure: the control plane
    leaves it to the runtime plane and converges on a later reconcile.
    """

    activation = task.activation
    if (
        activation.task_id is None
        or activation.source_task_log_id is None
        or activation.scheduled_for is None
    ):
        return "deferred"
    return "upsert"


def _scenario_activation_revision(
    *,
    client_name: str,
    deployment: str,
    scenario: Any,
    task: Any,
) -> str:
    """Stable generic task activation revision for a scenario schedule."""

    payload = {
        "client": client_name,
        "deployment": deployment,
        "scenario_id": scenario.scenario_id,
        "integration_slug": scenario.integration.package_slug,
        "schedule_id": task.id,
        "schedule": task.schedule.model_dump(mode="json"),
        "target": task.target.model_dump(mode="json"),
        "activation": task.activation.model_dump(mode="json"),
        "delivery": task.delivery.model_dump(mode="json"),
        "execution_mode": task.execution_mode,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
    ).hexdigest()[:16]
    return f"scenario-task:{task.id}:{digest}"


def _scenario_task_activation_payload(
    *,
    client_name: str,
    deployment: str,
    scenario: Any,
    task: Any,
) -> dict[str, Any]:
    """Serialize a scenario schedule as a generic task activation request."""

    activation = task.activation
    base = {
        "assistant_id": str(task.target.assistant_id),
        "task_id": activation.task_id,
        "source_task_log_id": activation.source_task_log_id,
        "activation_revision": _scenario_activation_revision(
            client_name=client_name,
            deployment=deployment,
            scenario=scenario,
            task=task,
        ),
        "scheduled_for": activation.scheduled_for,
        "execution_mode": task.execution_mode,
        "source_type": "scheduled",
        "task_label": activation.task_name,
        "task_summary": activation.task_description,
        "visibility_policy": activation.visibility_policy,
        "recurrence_hint": activation.recurrence_hint,
        "deployment_context": {
            "client": client_name,
            "deployment": deployment,
            "scenario_id": scenario.scenario_id,
            "schedule_id": task.id,
            "integration_slug": scenario.integration.package_slug,
            "entrypoint_function": activation.entrypoint_function,
            "target": task.target.model_dump(mode="json"),
            "delivery": task.delivery.model_dump(mode="json"),
            "requested_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    if _scenario_task_activation_action(task) == "deferred":
        missing = []
        if activation.task_id is None:
            missing.append("activation.task_id")
        if activation.source_task_log_id is None:
            missing.append("activation.source_task_log_id")
        if activation.scheduled_for is None:
            missing.append("activation.scheduled_for")
        base["deferred_reason"] = (
            "Scenario schedule is private to unity-deploy and its activation "
            "ids are not seeded yet; the runtime plane "
            "(materialize_runtime_state) seeds them in the woken assistant's "
            "own context. Deferring control-plane materialization until then: "
            f"missing {', '.join(missing)}"
        )
    return base


def apply_operations(operations: list[ReconcileOperation]) -> list[dict[str, Any]]:
    """Apply planned operations to Orchestra and return parsed responses."""
    import httpx

    from unity_deploy.utils.orchestra_client import OrchestraClientError, patch_json

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
        except OrchestraClientError as exc:
            if not (operation.missing_ok and exc.status_code == 404):
                raise
            reason = f"assistant target {operation.assistant_id} not found"
            missing_optional_assistants.add(operation.assistant_id)
            skip_missing(operation, reason)
        except httpx.HTTPStatusError as exc:
            if not (operation.missing_ok and exc.response.status_code == 404):
                raise
            reason = f"assistant target {operation.assistant_id} not found"
            missing_optional_assistants.add(operation.assistant_id)
            skip_missing(operation, reason)
    return responses


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
