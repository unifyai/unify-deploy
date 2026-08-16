"""Scheduled and offline task-execution materialization for Communication.

This module owns Cloud Tasks materialization, Orchestra admin calls, and
offline-run launches. An offline run is a plain one-shot Kubernetes Job
(``unity-task-execution-*``) carrying the full runner env: no AssistantSession,
no controller binding, no wake-reason envelope. The Job name is derived
from the run key, so dispatch retries and concurrent deliveries collapse
into a single execution via Kubernetes name uniqueness. Live session
lifecycle stays in ``infra/views.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Request
from google.api_core.exceptions import (
    AlreadyExists,
    NotFound as GcpNotFound,
    PermissionDenied as GcpPermissionDenied,
)
from google.protobuf import duration_pb2, timestamp_pb2
from kubernetes.client.rest import ApiException

from common.assistant_lookup import get_assistant, managed_desktop_entitled
from common.int_list_codec import encode_int_list_for_env
from common.team_summaries_codec import encode_team_summaries_for_env
from common.settings import SETTINGS
from common.task_destination import assistant_has_task_destination

# Single source of truth for the offline-runner subprocess contract.
# Imported from Unity so cold session wakes and warm in-pod spawns produce
# identical env-var dicts and run-keys for the same attempt.
from unify.task_scheduler.offline_runner_contract import (
    build_offline_run_key as _build_offline_run_key_shared,
    build_offline_runner_env as _build_offline_runner_env_shared,
)
from unify.task_scheduler.types.execution import Wake

from communication.dependencies import authorize_admin_or_assistant
from communication.infra.provider_event_dispatch import (
    ProviderEventDispatchAuthorizationError,
    ProviderEventDispatchOutcome,
    ProviderEventDispatchRequest,
    ProviderEventDispatchValidationError,
    dispatch_provider_event_offline,
    validate_provider_event_dispatch_request,
)
from .models import (
    OfflineTaskDispatchRequest,
    OfflineTaskJobTerminalRequest,
    ScheduledTaskExecutionDeleteRequest,
    ScheduledTaskExecutionUpsertRequest,
    TaskExecutionDiagnosticRequest,
)
from .assistant_sessions import (
    DESIRED_STATE_RUNNING,
    assistant_session_desired_state,
    binding_desktop_url,
    get_assistant_session,
    get_custom_objects_api,
    session_binding,
)
from .runtime_clients import (
    get_cloud_tasks_client as _get_cloud_tasks_client,
    get_k8s_clients as _get_k8s_clients,
)
from .self_router import assistant_self_router

router = APIRouter()
logger = logging.getLogger(__name__)

TASK_DUE_ENDPOINT_PATH = "/scheduled/tasks/due"
TASK_EXECUTION_REPAIR_PATH = "/infra/task-execution/repair"
OFFLINE_TASK_DISPATCH_PATH = "/infra/task-execution/offline-dispatch"
OFFLINE_TASK_JOB_TERMINAL_PATH = "/infra/offline-task/job-terminal"
PROVIDER_EVENT_DISPATCH_PATH = "/task-execution/provider-event-dispatch"
TASK_DUE_HTTP_TIMEOUT_SECONDS = 30
ORCHESTRA_TASK_MACHINE_PROJECT = "Assistants"
ORCHESTRA_TASK_EXECUTION_CURRENT_PATH = "/admin/task-execution/current"
ORCHESTRA_TASK_EXECUTION_REPROJECT_PATH = "/admin/task-execution/reproject"
ORCHESTRA_TASK_EXECUTION_CREATE_OR_ADOPT_PATH = "/admin/task-execution/create-or-adopt"
ORCHESTRA_TASK_EXECUTION_GET_PATH = "/admin/task-execution/get"
ORCHESTRA_TASK_EXECUTION_LATEST_PATH = "/admin/task-execution/latest"
ORCHESTRA_TASK_EXECUTION_UPDATE_PATH = "/admin/task-execution/update"
ORCHESTRA_TASK_SOURCE_RELEASE_PATH = "/admin/task-source/release-active"
OFFLINE_TASK_JOB_BACKOFF_LIMIT = 2
# Above SmartLead's 60s HTTP client timeout so SIGTERM writeback can finish
# before kubelet SIGKILLs the offline runner.
OFFLINE_TASK_TERMINATION_GRACE_PERIOD_SECONDS = 120
_INFLIGHT_RUN_STATES = frozenset({"pending", "running"})
# Live assistant conversation Jobs keep backoffLimit=0 (controller replaces
# work). Offline task Jobs need a small positive limit so a single transient
# pod crash can restart without waiting for the next scheduler delivery.
_TASK_ID_SAFE_RE = re.compile(r"[^a-z0-9-]+")
_task_queues_ensured: set[str] = set()


def _get_precreated_task_run(
    *,
    assistant_id: str,
    run_key: str,
    source_task_log_id: int | None = None,
) -> dict[str, Any] | None:
    """Fetch one pre-created Orchestra task run without creating or adopting."""

    payload: dict[str, Any] = {
        "project_name": ORCHESTRA_TASK_MACHINE_PROJECT,
        "assistant_id": assistant_id,
        "run_key": run_key,
    }
    if source_task_log_id is not None:
        payload["source_task_log_id"] = source_task_log_id
    body = _orchestra_admin_post(ORCHESTRA_TASK_EXECUTION_GET_PATH, payload)
    run = body.get("run")
    return run if isinstance(run, dict) else None


def _verify_precreated_provider_event_run(
    request: ProviderEventDispatchRequest,
) -> dict[str, Any]:
    """Require the Orchestra run referenced by one provider-event dispatch."""

    run = _get_precreated_task_run(
        assistant_id=request.assistant_id,
        run_key=request.run_key,
    )
    if run is None:
        raise ProviderEventDispatchValidationError("run_not_found")
    run_row_id = run.get("run_id")
    if run_row_id is None or int(run_row_id) != request.run_id:
        raise ProviderEventDispatchValidationError("run_id_mismatch")
    if str(run.get("run_key") or "") != request.run_key:
        raise ProviderEventDispatchValidationError("run_key_mismatch")
    run_task_id = run.get("task_id")
    if run_task_id is not None and int(run_task_id) != request.task_id:
        raise ProviderEventDispatchValidationError("run_task_id_mismatch")
    wake = run.get("wake")
    if wake is not None and str(wake) != request.wake:
        raise ProviderEventDispatchValidationError("run_wake_mismatch")
    delivery = run.get("delivery")
    if delivery is not None and str(delivery) != request.delivery:
        raise ProviderEventDispatchValidationError("run_delivery_mismatch")
    return run


def _provider_event_execution_metadata(
    request: ProviderEventDispatchRequest,
) -> dict[str, Any]:
    """Load execution metadata without rejecting stale lifecycle revisions."""

    execution = _lookup_current_task_execution(
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        destination=None,
    )
    return execution or {}


def _offline_dispatch_request_from_provider_event(
    request: ProviderEventDispatchRequest,
    *,
    execution: dict[str, Any],
) -> OfflineTaskDispatchRequest:
    """Adapt one provider-event dispatch request for offline job launch."""

    source_task_log_id = execution.get("source_task_log_id")
    requires_filesystem, requires_computer = _resolve_resource_flags(execution)
    return OfflineTaskDispatchRequest(
        assistant_id=request.assistant_id,
        destination=execution.get("destination"),
        task_id=request.task_id,
        source_task_log_id=int(source_task_log_id or request.task_id),
        revision=request.accepted_revision,
        delivery="offline",
        requires_filesystem=requires_filesystem,
        requires_computer=requires_computer,
        entrypoint=execution.get("entrypoint"),
        wake=Wake.provider_event,
        task_name=execution.get("task_name"),
    )


def _execute_provider_event_offline_dispatch(
    request: ProviderEventDispatchRequest,
    *,
    launch_job,
) -> ProviderEventDispatchOutcome:
    """Claim through Orchestra, then launch at most one offline job."""

    return dispatch_provider_event_offline(
        request=request,
        launch_job=launch_job,
        resolve_launch_identity=lambda req: _build_offline_task_job_name(req.run_key),
    )


def _emit_task_execution_event(event: str, **fields: Any) -> None:
    logger.info(
        "OBS_EVENT %s",
        json.dumps(
            {"event": event, **fields},
            sort_keys=True,
            default=str,
        ),
    )


def _resolve_resource_flags(
    data: Mapping[str, Any] | None,
    *,
    request_requires_filesystem: bool = False,
    request_requires_computer: bool = False,
) -> tuple[bool, bool]:
    data = data or {}
    requires_filesystem = bool(request_requires_filesystem) or bool(
        data.get("requires_filesystem"),
    )
    requires_computer = bool(request_requires_computer) or bool(
        data.get("requires_computer"),
    )
    return requires_filesystem, requires_computer


def _offline_dispatch_event_fields(
    request: OfflineTaskDispatchRequest,
    *,
    stage: str,
    run_key: str | None = None,
    job_name: str | None = None,
    status: str | None = None,
    run_state: str | None = None,
    stale_reason: str | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "stage": stage,
        "assistant_id": request.assistant_id,
        "task_id": request.task_id,
        "source_task_log_id": request.source_task_log_id,
        "wake": str(request.wake),
        "delivery": request.delivery,
        "revision": request.revision,
        "scheduled_for": _request_scheduled_for_iso(request),
    }
    if run_key is not None:
        fields["run_key"] = run_key
    if job_name is not None:
        fields["job_name"] = job_name
    if status is not None:
        fields["status"] = status
    if run_state is not None:
        fields["run_state"] = run_state
    if stale_reason is not None:
        fields["stale_reason"] = stale_reason
    if error is not None:
        fields["error_type"] = type(error).__name__
        fields["error"] = str(error)
    return fields


def _required_contact_id(assistant_data: dict[str, Any], field_name: str) -> int:
    """Return a resolved contact id required by offline Unity launches."""
    value = assistant_data.get(field_name)
    if value is None:
        assistant_id = assistant_data.get("assistant_id") or assistant_data.get(
            "agent_id",
        )
        raise RuntimeError(
            f"Assistant {assistant_id} is missing required {field_name}",
        )
    return int(value)


def _task_due_queue_parent() -> str:
    """Return the Cloud Tasks parent resource for scheduled task queues."""

    return (
        f"projects/{SETTINGS.gcp_project_id}/locations/"
        f"{SETTINGS.task_due_queue_location}"
    )


def _task_queue_path(queue_name: str) -> str:
    """Return the fully qualified Cloud Tasks queue path."""

    return f"{_task_due_queue_parent()}/queues/{queue_name}"


def _normalize_task_id_component(value: str) -> str:
    """Normalize free-form identifiers into Cloud Tasks task-id fragments."""

    normalized = _TASK_ID_SAFE_RE.sub("-", value.lower()).strip("-")
    return normalized or "assistant"


def _scheduled_execution_task_name(
    *,
    assistant_id: str,
    task_id: int,
    revision: str,
    scheduled_for: datetime,
    delivery: str = "live",
    queue_name: str | None = None,
) -> str:
    """Return the Cloud Tasks name for one scheduled execution delivery."""

    due_utc = scheduled_for.astimezone(timezone.utc)
    assistant_component = _normalize_task_id_component(str(assistant_id))[:32]
    revision_component = hashlib.sha256(
        revision.encode("utf-8"),
    ).hexdigest()[:10]
    task_component = (
        f"task-{delivery}-{assistant_component}-{task_id}-"
        f"{due_utc.strftime('%Y%m%d%H%M%S')}-{revision_component}"
    )
    queue_path = _task_queue_path(queue_name or SETTINGS.task_due_queue_name)
    return f"{queue_path}/tasks/{task_component}"


def _ensure_task_queue(queue_name: str) -> str:
    """Create one scheduled-task queue on first use if it is missing."""

    queue_path = _task_queue_path(queue_name)
    if queue_name in _task_queues_ensured:
        return queue_path

    client = _get_cloud_tasks_client()
    try:
        client.get_queue(name=queue_path)
    except GcpNotFound:
        from google.cloud import tasks_v2

        queue = tasks_v2.Queue(name=queue_path)
        try:
            client.create_queue(parent=_task_due_queue_parent(), queue=queue)
        except AlreadyExists:
            pass

    _task_queues_ensured.add(queue_name)
    return queue_path


def _task_queue_diagnostics() -> list[dict[str, Any]]:
    """Return existence diagnostics for required task execution queues."""

    client = _get_cloud_tasks_client()
    diagnostics: list[dict[str, Any]] = []
    for queue_name in dict.fromkeys(
        [
            SETTINGS.task_due_queue_name,
            SETTINGS.task_offline_queue_name,
            SETTINGS.task_execution_repair_queue_name,
        ],
    ):
        queue_path = _task_queue_path(queue_name)
        try:
            client.get_queue(name=queue_path)
            status = "ok"
            error = None
        except GcpNotFound as exc:
            status = "missing"
            error = str(exc)
        except GcpPermissionDenied as exc:
            status = "permission_denied"
            error = str(exc)
        diagnostics.append(
            {
                "queue_name": queue_name,
                "queue_path": queue_path,
                "status": status,
                "error": error,
            },
        )
    return diagnostics


def _cloud_task_diagnostic(task_name: str) -> dict[str, Any]:
    """Report whether the expected Cloud Task currently exists."""

    client = _get_cloud_tasks_client()
    try:
        client.get_task(name=task_name)
        return {"cloud_task_status": "present", "cloud_task_error": None}
    except GcpNotFound as exc:
        return {"cloud_task_status": "missing", "cloud_task_error": str(exc)}
    except GcpPermissionDenied as exc:
        return {
            "cloud_task_status": "permission_denied",
            "cloud_task_error": str(exc),
        }


def _scheduled_execution_http_body(
    request: ScheduledTaskExecutionUpsertRequest,
) -> bytes:
    """Serialize the delayed task delivery payload."""

    payload = {
        "assistant_id": request.assistant_id,
        "task_id": request.task_id,
        "source_task_log_id": request.source_task_log_id,
        "revision": request.revision,
        "scheduled_for": request.scheduled_for.astimezone(timezone.utc).isoformat(),
        "delivery": request.delivery,
        "requires_filesystem": request.requires_filesystem,
        "requires_computer": request.requires_computer,
        "entrypoint": request.entrypoint,
        "wake": str(request.wake),
        "task_label": request.task_label or "",
        "task_summary": request.task_summary or "",
        "visibility_policy": request.visibility_policy,
        "recurrence_hint": request.recurrence_hint,
    }
    if request.destination is not None:
        payload["destination"] = request.destination
    return json.dumps(payload).encode("utf-8")


def _scheduled_execution_queue_candidates(delivery: str) -> list[str]:
    """Return every queue that may currently hold an execution task."""

    normalized_mode = "offline" if delivery == "offline" else "live"
    candidates = [
        SETTINGS.task_execution_repair_queue_name,
        (
            SETTINGS.task_due_queue_name
            if normalized_mode == "live"
            else SETTINGS.task_offline_queue_name
        ),
    ]
    return list(dict.fromkeys(candidates))


def _scheduled_execution_target(
    request: ScheduledTaskExecutionUpsertRequest,
) -> tuple[str, str, datetime]:
    """Choose the queue, callback URL, and checkpoint time for this execution."""

    # `scheduled_for` is the canonical occurrence; jitter is a dispatch-time
    # offset so the recorded time stays usable as an identity and as the
    # anchor for deriving the next slot.
    scheduled_for = request.scheduled_for.astimezone(timezone.utc) + timedelta(
        seconds=request.dispatch_offset_seconds or 0.0,
    )
    horizon_cutoff = datetime.now(timezone.utc) + timedelta(
        days=SETTINGS.task_execution_horizon_days,
    )
    if scheduled_for > horizon_cutoff:
        return (
            SETTINGS.task_execution_repair_queue_name,
            f"{SETTINGS.comms_url}{TASK_EXECUTION_REPAIR_PATH}",
            horizon_cutoff,
        )
    if request.delivery == "offline":
        return (
            SETTINGS.task_offline_queue_name,
            f"{SETTINGS.comms_url}{OFFLINE_TASK_DISPATCH_PATH}",
            scheduled_for,
        )
    return (
        SETTINGS.task_due_queue_name,
        f"{SETTINGS.adapters_url}{TASK_DUE_ENDPOINT_PATH}",
        scheduled_for,
    )


def _delete_cloud_task_if_present(task_name: str) -> bool:
    """Delete one Cloud Task when present."""

    client = _get_cloud_tasks_client()
    try:
        client.delete_task(name=task_name)
        return True
    except GcpNotFound:
        return False


def _delete_scheduled_execution_task(
    *,
    assistant_id: str,
    task_id: int,
    revision: str,
    scheduled_for: datetime,
    delivery: str = "live",
) -> bool:
    """Delete one delayed execution task if it still exists."""

    deleted = False
    for queue_name in _scheduled_execution_queue_candidates(delivery):
        task_name = _scheduled_execution_task_name(
            assistant_id=assistant_id,
            task_id=task_id,
            revision=revision,
            scheduled_for=scheduled_for,
            delivery=delivery,
            queue_name=queue_name,
        )
        deleted = _delete_cloud_task_if_present(task_name) or deleted
    return deleted


def _require_cloud_task_present(task_name: str) -> None:
    """Fail materialization if the expected Cloud Task cannot be read back."""

    client = _get_cloud_tasks_client()
    try:
        client.get_task(name=task_name)
    except GcpNotFound as exc:
        raise RuntimeError(
            f"Cloud Task materialization missing after upsert: {task_name}",
        ) from exc


def _upsert_scheduled_execution_task(
    request: ScheduledTaskExecutionUpsertRequest,
) -> dict[str, Any]:
    """Create or repair the Cloud Task for one scheduled execution."""

    if not SETTINGS.orchestra_admin_key:
        raise RuntimeError("ORCHESTRA_ADMIN_KEY must be configured")
    if request.delivery == "live" and not SETTINGS.adapters_url:
        raise RuntimeError("UNIFY_ADAPTERS_URL must be configured")
    if request.delivery == "offline" and not SETTINGS.comms_url:
        raise RuntimeError("UNIFY_COMMS_URL must be configured")

    from google.cloud import tasks_v2

    queue_name, target_url, schedule_at = _scheduled_execution_target(request)
    if target_url.startswith("/"):
        raise RuntimeError(
            "Target service URL must be configured for task execution materialization",
        )
    queue_path = _ensure_task_queue(queue_name)
    task_name = _scheduled_execution_task_name(
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        revision=request.revision,
        scheduled_for=request.scheduled_for,
        delivery=request.delivery,
        queue_name=queue_name,
    )
    schedule_time = timestamp_pb2.Timestamp()
    schedule_time.FromDatetime(schedule_at.astimezone(timezone.utc))

    task = tasks_v2.Task(
        name=task_name,
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=target_url,
            headers={
                "Authorization": f"Bearer {SETTINGS.orchestra_admin_key}",
                "Content-Type": "application/json",
            },
            body=_scheduled_execution_http_body(request),
        ),
        schedule_time=schedule_time,
        dispatch_deadline=duration_pb2.Duration(
            seconds=SETTINGS.task_due_dispatch_deadline_seconds,
        ),
    )

    client = _get_cloud_tasks_client()
    try:
        client.create_task(parent=queue_path, task=task)
        action = "created"
    except AlreadyExists:
        action = "already_exists"

    _require_cloud_task_present(task_name)

    return {
        "action": action,
        "queue": queue_path,
        "task_name": task_name,
        "scheduled_for": request.scheduled_for.astimezone(timezone.utc).isoformat(),
        "scheduled_checkpoint_for": schedule_at.astimezone(timezone.utc).isoformat(),
        "target_url": target_url,
    }


def _orchestra_admin_headers() -> dict[str, str]:
    """Return auth headers for Orchestra admin task-machine APIs."""

    if not SETTINGS.orchestra_admin_key:
        raise RuntimeError("ORCHESTRA_ADMIN_KEY must be configured")
    return {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}


def _orchestra_admin_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Call one Orchestra admin endpoint and decode the JSON body."""

    if not SETTINGS.orchestra_url:
        raise RuntimeError("ORCHESTRA_URL must be configured")
    response = requests.post(
        f"{SETTINGS.orchestra_url}{path}",
        json=payload,
        headers=_orchestra_admin_headers(),
        timeout=TASK_DUE_HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected Orchestra response for {path}")
    return body


def _lookup_current_task_execution(
    *,
    assistant_id: str,
    task_id: int,
    destination: str | None = None,
) -> dict[str, Any] | None:
    """Fetch the current projected execution row for one assistant/task pair."""

    payload = {
        "project_name": ORCHESTRA_TASK_MACHINE_PROJECT,
        "assistant_id": assistant_id,
        "task_id": task_id,
    }
    if destination is not None:
        payload["destination"] = destination
    body = _orchestra_admin_post(
        ORCHESTRA_TASK_EXECUTION_CURRENT_PATH,
        payload,
    )
    execution = body.get("execution")
    return execution if isinstance(execution, dict) else None


def _execution_snapshot_from_explicit_dispatch_request(
    request: OfflineTaskDispatchRequest,
) -> dict[str, Any]:
    """Build an execution-shaped snapshot from an explicit dispatch request.

    Explicit REST triggers are issued by Orchestra immediately after it resolves
    the current execution. Re-fetching ``/admin/task-execution/current`` is a
    redundant round-trip (and previously contended on the still-open resolve
    transaction). Trust the caller-supplied fields for launch + validation.
    """

    snapshot: dict[str, Any] = {
        "assistant_id": request.assistant_id,
        "task_id": request.task_id,
        "source_task_log_id": request.source_task_log_id,
        "revision": request.revision,
        "delivery": request.delivery,
        "destination": request.destination,
        "entrypoint": request.entrypoint,
        "max_runtime_seconds": request.max_runtime_seconds,
        "task_name": request.task_name,
    }
    if request.scheduled_for is not None:
        snapshot["scheduled_for"] = request.scheduled_for.astimezone(
            timezone.utc,
        ).isoformat()
    return snapshot


def _resolve_offline_dispatch_execution(
    request: OfflineTaskDispatchRequest,
) -> dict[str, Any] | None:
    """Return the execution used to validate and launch one offline dispatch."""

    if request.wake == "explicit":
        return _execution_snapshot_from_explicit_dispatch_request(request)
    return _lookup_current_task_execution(
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        destination=request.destination,
    )


def _reproject_task_execution(
    *,
    assistant_id: str,
    task_id: int,
) -> dict[str, Any]:
    """Ask Orchestra to rebuild the current execution projection for one task."""

    return _orchestra_admin_post(
        ORCHESTRA_TASK_EXECUTION_REPROJECT_PATH,
        {
            "project_name": ORCHESTRA_TASK_MACHINE_PROJECT,
            "assistant_id": assistant_id,
            "task_id": task_id,
        },
    )


def _lookup_latest_task_run(
    *,
    assistant_id: str,
    task_id: int,
    source_task_log_id: int | None = None,
) -> dict[str, Any] | None:
    """Fetch the latest task run row for one assistant/task pair."""

    payload: dict[str, Any] = {
        "project_name": ORCHESTRA_TASK_MACHINE_PROJECT,
        "assistant_id": assistant_id,
        "task_id": task_id,
    }
    if source_task_log_id is not None:
        payload["source_task_log_id"] = source_task_log_id
    body = _orchestra_admin_post(ORCHESTRA_TASK_EXECUTION_LATEST_PATH, payload)
    run = body.get("run")
    return run if isinstance(run, dict) else None


def _get_assistant_data(assistant_id: str) -> dict[str, Any]:
    """Return assistant metadata from Orchestra for execution authorization."""

    assistant_data = get_assistant(assistant_id=assistant_id)
    if not assistant_data or not assistant_data.get("assistant_id"):
        raise RuntimeError(f"Assistant {assistant_id} no longer exists")
    return assistant_data


def _create_or_adopt_task_run(payload: dict[str, Any]) -> dict[str, Any]:
    """Create or adopt one offline task run row."""

    request_payload = {"project_name": ORCHESTRA_TASK_MACHINE_PROJECT, **payload}
    return _orchestra_admin_post(
        ORCHESTRA_TASK_EXECUTION_CREATE_OR_ADOPT_PATH,
        request_payload,
    )


def _update_task_run(
    *,
    assistant_id: str,
    run_key: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Apply a partial update to one task run row."""

    return _orchestra_admin_post(
        ORCHESTRA_TASK_EXECUTION_UPDATE_PATH,
        {
            "project_name": ORCHESTRA_TASK_MACHINE_PROJECT,
            "assistant_id": assistant_id,
            "run_key": run_key,
            "updates": updates,
        },
    )


def _release_active_task_source(
    *,
    assistant_id: str,
    source_task_log_id: int,
    mode: str,
    info: str | None = None,
    run_key: str | None = None,
) -> dict[str, Any]:
    """Terminalize executions left running after their offline worker vanished.

    ``mode="reopen"`` is used before retrying the same ``source_task_log_id``.
    ``mode="fail"`` is for terminal crash writeback without an immediate retry.

    Pass ``run_key`` whenever the caller knows which run it is finishing.
    Unscoped, this releases every running execution under the definition,
    including a successor occurrence that recurrence has already started.
    """

    payload: dict[str, Any] = {
        "project_name": ORCHESTRA_TASK_MACHINE_PROJECT,
        "assistant_id": assistant_id,
        "source_task_log_id": int(source_task_log_id),
        "mode": mode,
    }
    if info:
        payload["info"] = info
    if run_key:
        payload["run_key"] = run_key
    return _orchestra_admin_post(ORCHESTRA_TASK_SOURCE_RELEASE_PATH, payload)


def _fail_stale_inflight_run_and_reopen_source(
    *,
    assistant_id: str,
    source_task_log_id: int,
    run_key: str,
    run_state: str,
    job_status: dict[str, Any],
    retry_count: int | None = None,
) -> str:
    """Fail a stale Run row and reopen its Tasks source for reclaim."""

    error = _stale_inflight_run_error(
        run_key=run_key,
        run_state=run_state,
        job_status=job_status,
    )
    _update_task_run(
        assistant_id=assistant_id,
        run_key=run_key,
        updates=_failed_task_run_updates(
            error=error,
            result_summary=error,
            retry_count=retry_count,
        ),
    )
    _release_active_task_source(
        assistant_id=assistant_id,
        source_task_log_id=source_task_log_id,
        mode="reopen",
        info=error,
    )
    return error


def _active_sibling_run_for_source(
    *,
    batch_api: Any,
    request: OfflineTaskDispatchRequest,
    exclude_run_key: str,
) -> dict[str, Any] | None:
    """Return the sibling run genuinely executing this Tasks source, if any.

    "Genuinely executing" means an in-flight run row whose recorded Job is
    still active on the cluster — a stale row whose Job vanished does not
    count and is handled by the stale-inflight repair path instead.
    """

    latest = _lookup_latest_task_run(
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        source_task_log_id=request.source_task_log_id,
    )
    if not isinstance(latest, dict):
        return None
    other_run_key = str(latest.get("run_key") or "")
    if not other_run_key or other_run_key == exclude_run_key:
        return None
    if str(latest.get("state") or "") not in _INFLIGHT_RUN_STATES:
        return None
    job_name = str(latest.get("job_name") or "")
    if not job_name:
        return None
    job_status = _classify_offline_job_status(batch_api, job_name)
    if job_status.get("status") != "active":
        return None
    return {
        "run_key": other_run_key,
        "job_name": job_name,
        "run_state": str(latest.get("state") or ""),
        "job_status": job_status,
    }


def _skip_overlapped_scheduled_occurrence(
    *,
    batch_api: Any,
    request: OfflineTaskDispatchRequest,
    run_key: str,
    execution: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Terminalize a due scheduled occurrence whose predecessor still runs.

    Booting a worker just to discover the overlap costs a full pod cold
    start; this check is one run lookup and one Job status read. Only
    ``scheduled`` wakes are eligible — explicit kicks, triggers, and
    provider events are content-bearing and must never be swallowed.

    The skipped occurrence terminalizes as completed with an overlap
    summary. Its successor is deliberately not minted here: projection
    ownership stays with the running predecessor's dispatcher, and the
    supervisor sweep floors the series once that run ends.
    """

    if str(request.wake) != "scheduled":
        return None
    sibling = _active_sibling_run_for_source(
        batch_api=batch_api,
        request=request,
        exclude_run_key=run_key,
    )
    if sibling is None:
        return None
    _create_or_adopt_task_run(
        _build_offline_run_create_payload(request, run_key, execution),
    )
    _update_task_run(
        assistant_id=request.assistant_id,
        run_key=run_key,
        updates=_completed_task_run_updates(
            result_summary=(
                "overlap_skip: predecessor run "
                f"{sibling['run_key']} (job {sibling['job_name']}) is still "
                "running; occurrence skipped at dispatch without booting a "
                "worker"
            ),
        ),
    )
    return {
        "success": True,
        "status": "skipped_overlap",
        "run_key": run_key,
        "predecessor_run_key": sibling["run_key"],
        "predecessor_job_name": sibling["job_name"],
    }


def _adopt_inflight_source_conflict(
    *,
    batch_api: Any,
    request: OfflineTaskDispatchRequest,
    exclude_run_key: str,
) -> dict[str, Any] | None:
    """Return an adopt payload when another Job already owns this Tasks source."""

    sibling = _active_sibling_run_for_source(
        batch_api=batch_api,
        request=request,
        exclude_run_key=exclude_run_key,
    )
    if sibling is None:
        return None
    return {
        "success": True,
        "status": "adopted_inflight_source",
        "run_key": sibling["run_key"],
        "job_name": sibling["job_name"],
        "run_state": sibling["run_state"],
        "job_status": sibling["job_status"],
        "source_task_log_id": request.source_task_log_id,
    }


def _running_task_run_updates(
    job_name: str,
    *,
    retry_count: int | None = None,
) -> dict[str, Any]:
    """Return the canonical Orchestra patch for one in-flight offline run."""

    updates: dict[str, Any] = {
        "state": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "job_name": job_name,
        "completed_at": None,
        "error": None,
        "result_summary": None,
    }
    if retry_count is not None:
        updates["retry_count"] = retry_count
    return updates


def _failed_task_run_updates(
    *,
    error: str,
    result_summary: str,
    retry_count: int | None = None,
) -> dict[str, Any]:
    """Return the canonical terminal patch for a failed offline run."""

    updates: dict[str, Any] = {
        "state": "failed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
        "result_summary": result_summary,
    }
    if retry_count is not None:
        updates["retry_count"] = retry_count
    return updates


def _completed_task_run_updates(
    *,
    result_summary: str | None = None,
) -> dict[str, Any]:
    """Return the canonical terminal patch for a completed offline run."""

    updates: dict[str, Any] = {
        "state": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }
    if result_summary is not None:
        updates["result_summary"] = result_summary
    return updates


def _terminalize_offline_job_outcome(
    *,
    assistant_id: str,
    run_key: str,
    source_task_log_id: int,
    job_name: str,
    terminal_type: str,
) -> dict[str, Any]:
    """Idempotently mirror a terminal Kubernetes Job onto Tasks/Executions.

    Kubernetes Job terminal state is the source of truth. In-pod SIGTERM
    writeback may already have finalized both rows; release-active and
    conditional Run updates no-op in that case.
    """

    normalized = str(terminal_type or "").strip()
    if normalized not in {"Complete", "Failed"}:
        raise ValueError(
            f"terminal_type must be 'Complete' or 'Failed', got {terminal_type!r}.",
        )

    run = _get_precreated_task_run(
        assistant_id=assistant_id,
        run_key=run_key,
        source_task_log_id=source_task_log_id,
    )
    run_state = str((run or {}).get("state") or "")
    run_updated = False
    info = f"Kubernetes Job {job_name} reached terminal condition={normalized}"

    if normalized == "Failed":
        if run_state in _INFLIGHT_RUN_STATES:
            _update_task_run(
                assistant_id=assistant_id,
                run_key=run_key,
                updates=_failed_task_run_updates(
                    error=info,
                    result_summary=info,
                ),
            )
            run_updated = True
        release = _release_active_task_source(
            assistant_id=assistant_id,
            source_task_log_id=source_task_log_id,
            mode="fail",
            info=info,
            run_key=run_key,
        )
    else:
        if run_state in _INFLIGHT_RUN_STATES:
            _update_task_run(
                assistant_id=assistant_id,
                run_key=run_key,
                updates=_completed_task_run_updates(
                    result_summary=(
                        f"Job {job_name} Complete while Run was still {run_state}; "
                        "assumed in-pod success writeback lost the race"
                    ),
                ),
            )
            run_updated = True
        # No release on the success path. The run this Job owns was just
        # terminalized by run_key, and release is scoped to the definition:
        # it fails *every* running execution under it. Recurrence projects the
        # next occurrence at dispatch, so by the time a Job completes its
        # successor is often already running, and releasing here would kill it
        # — one healthy run silently ending the series.
        release = None

    return {
        "success": True,
        "terminal_type": normalized,
        "run_key": run_key,
        "job_name": job_name,
        "run_state_before": run_state or None,
        "run_updated": run_updated,
        "source_release": release,
    }


def _job_condition_status(job: Any, condition_type: str) -> bool:
    """Return whether a Kubernetes Job exposes a true condition."""

    for condition in getattr(getattr(job, "status", None), "conditions", None) or []:
        if (
            str(getattr(condition, "type", "") or "") == condition_type
            and str(getattr(condition, "status", "") or "") == "True"
        ):
            return True
    return False


def _job_start_time(job: Any) -> datetime | None:
    """Return the Kubernetes Job start time as an aware datetime."""

    start_time = getattr(getattr(job, "status", None), "start_time", None)
    if start_time is None:
        start_time = getattr(getattr(job, "status", None), "startTime", None)
    if isinstance(start_time, datetime):
        return start_time.astimezone(timezone.utc)
    if isinstance(start_time, str) and start_time:
        return datetime.fromisoformat(start_time.replace("Z", "+00:00")).astimezone(
            timezone.utc,
        )
    return None


def _classify_offline_job_status(
    batch_api: Any,
    job_name: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify whether a stored offline job reference is still executable."""

    try:
        job = batch_api.read_namespaced_job(
            name=job_name,
            namespace=SETTINGS.default_namespace,
        )
    except ApiException as exc:
        if exc.status == 404:
            return {"status": "missing", "job_name": job_name}
        return {
            "status": "unknown",
            "job_name": job_name,
            "reason": f"kubernetes_api_error_{exc.status}",
        }

    status = getattr(job, "status", None)
    spec = getattr(job, "spec", None)
    active = int(getattr(status, "active", None) or 0)
    succeeded = int(getattr(status, "succeeded", None) or 0)
    failed = int(getattr(status, "failed", None) or 0)
    started_at = _job_start_time(job)
    # The Job's own activeDeadlineSeconds is the task's max_runtime_seconds;
    # unbounded tasks (no deadline on the Job) are never stale by age.
    deadline_seconds = int(getattr(spec, "active_deadline_seconds", None) or 0)
    if active > 0:
        current_time = now or datetime.now(timezone.utc)
        if started_at is not None and deadline_seconds > 0:
            age_seconds = (current_time - started_at).total_seconds()
            if age_seconds > deadline_seconds:
                return {
                    "status": "stale",
                    "job_name": job_name,
                    "active": active,
                    "started_at": started_at.isoformat(),
                    "age_seconds": age_seconds,
                    "deadline_seconds": deadline_seconds,
                }
        return {
            "status": "active",
            "job_name": job_name,
            "active": active,
            "started_at": started_at.isoformat() if started_at else None,
            "deadline_seconds": deadline_seconds or None,
        }
    if succeeded > 0 or _job_condition_status(job, "Complete"):
        return {
            "status": "completed",
            "job_name": job_name,
            "succeeded": succeeded,
        }
    if failed > 0 or _job_condition_status(job, "Failed"):
        return {"status": "failed", "job_name": job_name, "failed": failed}
    return {"status": "inactive", "job_name": job_name}


def _stale_inflight_run_error(
    *,
    run_key: str,
    run_state: str,
    job_status: dict[str, Any],
) -> str:
    """Build a compact reconciliation error for an in-flight run without live work."""

    return (
        "Offline task run lost live execution evidence: "
        f"run_key={run_key}, state={run_state}, "
        f"job_status={job_status.get('status')}, "
        f"job_name={job_status.get('job_name')}"
    )


def _offline_job_lifecycle_safeguards() -> dict[str, Any]:
    """Return the Kubernetes safeguards required for offline task jobs."""

    return {
        "active_deadline_seconds": (
            "per-task max_runtime_seconds, floored at "
            f"{SETTINGS.offline_task_max_runtime_seconds}s"
        ),
        "ttl_seconds_after_finished": SETTINGS.offline_task_job_ttl_seconds,
        "backoff_limit": OFFLINE_TASK_JOB_BACKOFF_LIMIT,
        "durable_terminal_state": "Tasks/Executions and Tasks rows",
    }


def _task_execution_health_summary(
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return compact alertable counts for task execution diagnostics."""

    statuses: dict[str, int] = {}
    blocking_conditions: dict[str, int] = {}
    repairable = 0
    for diagnostic in diagnostics:
        health = diagnostic.get("health") or {}
        status = str(health.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        if health.get("repairable"):
            repairable += 1
        for condition in diagnostic.get("blocking_conditions") or []:
            condition_type = str(condition.get("type") or "unknown")
            blocking_conditions[condition_type] = (
                blocking_conditions.get(condition_type, 0) + 1
            )
    return {
        "total": len(diagnostics),
        "repairable": repairable,
        "statuses": statuses,
        "blocking_conditions": blocking_conditions,
        "job_lifecycle_safeguards": _offline_job_lifecycle_safeguards(),
    }


def _optional_display_text(value: Any) -> str | None:
    """Normalize optional display text so empty strings do not leak into rows."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_datetime_string(value: str | None) -> str | None:
    """Normalize ISO-8601 datetimes for equality checks."""

    if not value:
        return None
    try:
        return (
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .isoformat()
        )
    except ValueError:
        return str(value)


def _validate_current_offline_execution(
    request: OfflineTaskDispatchRequest,
    execution: dict[str, Any] | None,
) -> str | None:
    """Return a stale reason when the dispatch no longer matches the current execution."""

    if execution is None:
        return "execution_missing"
    # Manual REST triggers may fire any offline execution (scheduled or
    # communication-triggered). Other dispatches still require kind match.
    wake = Wake.normalize(request.wake)
    if wake is not Wake.explicit:
        if execution.get("wake") != str(wake):
            return "wake_changed"
    if execution.get("delivery") != "offline":
        return "delivery_changed"
    if execution.get("revision") != request.revision:
        return "revision_mismatch"
    if execution.get("destination") != request.destination:
        return "destination_mismatch"
    if int(execution.get("source_task_log_id") or 0) != request.source_task_log_id:
        return "source_task_log_id_mismatch"
    execution_entrypoint = execution.get("entrypoint")
    if execution_entrypoint is None and request.entrypoint is not None:
        return "entrypoint_mismatch"
    if (
        execution_entrypoint is not None
        and request.entrypoint is not None
        and int(execution_entrypoint) != int(request.entrypoint)
    ):
        return "entrypoint_mismatch"
    if wake is Wake.scheduled and _normalize_datetime_string(
        execution.get("scheduled_for"),
    ) != _normalize_datetime_string(_request_scheduled_for_iso(request)):
        return "scheduled_for_mismatch"
    return None


def _request_scheduled_for_iso(request: OfflineTaskDispatchRequest) -> str | None:
    """Return the request's scheduled timestamp in canonical UTC ISO format."""

    if request.scheduled_for is None:
        return None
    return request.scheduled_for.astimezone(timezone.utc).isoformat()


def _build_offline_run_key(request: OfflineTaskDispatchRequest) -> str:
    """Build a stable idempotency key for one offline execution attempt.

    Thin adapter over the shared
    :func:`unify.task_scheduler.offline_runner_contract.build_offline_run_key`
    so the hosted K8s path and the local in-process path produce
    identical keys for the same attempt. If those keys ever diverged
    Orchestra's create-or-adopt path would fail to deduplicate
    concurrent attempts across topologies.

    Shared-team tasks insert a normalised destination segment between
    the assistant id and task id so concurrent attempts for different
    destinations never collide.
    """

    run_key = _build_offline_run_key_shared(
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        revision=request.revision,
        wake=str(request.wake),
        scheduled_for=request.scheduled_for,
        source_contact_id=request.source_contact_id,
        source_medium=request.source_medium,
        source_ref=request.source_ref,
    )
    if not request.destination:
        return run_key
    destination_part = f"{_normalize_task_id_component(request.destination)}:"
    prefix = f"offline:{request.wake}:{request.assistant_id}:"
    if run_key.startswith(prefix):
        return f"{prefix}{destination_part}{run_key[len(prefix):]}"
    return run_key


def _resolve_offline_dispatch_run_key(
    request: OfflineTaskDispatchRequest,
    execution: dict[str, Any] | None,
) -> str:
    """Return the run key naming this occurrence.

    A projected scheduled occurrence already carries the run key Orchestra
    minted for it, and validation has just pinned this dispatch to that
    exact occurrence: revision, destination, source row, entrypoint, and
    slot all matched. Adopting the stored key therefore names the same run
    the projection named, instead of rebuilding it through a second
    byte-compatible implementation. Two normalisation drifts in that dual
    construction (``team:11`` vs ``team-11``; two datetime spellings) minted
    twins and silently halted the scheduler in July.

    The other lanes still construct. Their occurrences are named from
    inbound facts the ledger has not recorded yet: a triggered wake keys on
    the arriving message, an explicit kick has no projected row at all, and
    provider events key on the event identity digest.
    """

    if Wake.normalize(request.wake) is Wake.scheduled:
        stored = str((execution or {}).get("run_key") or "").strip()
        if stored:
            return stored
    return _build_offline_run_key(request)


def _build_offline_task_job_name(
    run_key: str,
    *,
    retry_count: int | None = None,
) -> str:
    """Return the deterministic Kubernetes Job name for one offline run attempt.

    The name is a stable function of the run key, so concurrent deliveries of
    the same attempt collapse into one Job via Kubernetes name uniqueness.
    Retries salt the digest with the retry count because the failed Job object
    may still exist (TTL pending) under the previous name.
    """

    seed = run_key if not retry_count else f"{run_key}:retry:{retry_count}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    base_name = f"unity-task-execution-{digest}"
    suffix = SETTINGS.env_suffix.lstrip("-")
    return f"{base_name}-{suffix}" if suffix else base_name


def _create_or_replace_offline_env_secret(
    core_api: Any,
    *,
    job_name: str,
    run_key: str,
    offline_env: dict[str, str],
) -> None:
    """Write the per-run env Secret consumed by the task-execution Job via envFrom.

    The Secret shares the Job's name; after the Job is created it becomes the
    Secret's owner so both garbage-collect together. Keeping the runner env
    (including the assistant's UNIFY_KEY) in a Secret rather than inline pod
    spec env keeps credentials out of Job/pod describe output.
    """

    from kubernetes import client as k8s_client

    body = k8s_client.V1Secret(
        metadata=k8s_client.V1ObjectMeta(
            name=job_name,
            namespace=SETTINGS.default_namespace,
            labels={"app": "unity-task-execution"},
            annotations={"unify.ai/task-execution-key": run_key},
        ),
        type="Opaque",
        string_data={key: str(value) for key, value in offline_env.items()},
    )
    try:
        core_api.create_namespaced_secret(
            namespace=SETTINGS.default_namespace,
            body=body,
        )
    except ApiException as exc:
        if exc.status != 409:
            raise
        core_api.replace_namespaced_secret(
            name=job_name,
            namespace=SETTINGS.default_namespace,
            body=body,
        )


def _adopt_offline_env_secret(
    core_api: Any,
    *,
    job_name: str,
    job: Any,
) -> None:
    """Make the created Job own the env Secret so both garbage-collect together."""

    core_api.patch_namespaced_secret(
        name=job_name,
        namespace=SETTINGS.default_namespace,
        body={
            "metadata": {
                "ownerReferences": [
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "name": job.metadata.name,
                        "uid": job.metadata.uid,
                    },
                ],
            },
        },
    )


def _bounded_offline_runtime(max_runtime_seconds: int | None) -> int:
    """Return the run's deadline, floored against the platform ceiling.

    A task may bound itself tighter than the platform does; it may not opt
    out. Asking for nothing is the common case -- `max_runtime_seconds`
    defaults to None on every task and nothing sets it -- and used to mean
    unbounded, which is how offline jobs reached eight days.
    """

    ceiling = int(SETTINGS.offline_task_max_runtime_seconds)
    if max_runtime_seconds is None:
        return ceiling
    requested = int(max_runtime_seconds)
    return min(requested, ceiling) if requested > 0 else ceiling


def _launch_offline_task_job(
    *,
    batch_api: Any,
    core_api: Any,
    request: OfflineTaskDispatchRequest,
    run_key: str,
    job_name: str,
    offline_env: dict[str, str],
    max_runtime_seconds: int | None,
) -> bool:
    """Create the one-shot Kubernetes Job for one offline run attempt.

    ``max_runtime_seconds`` is the task's own execution bound, floored against
    the platform ceiling: a task may ask for less than
    ``offline_task_max_runtime_seconds`` but never more, and one that asks for
    nothing gets the ceiling rather than forever. Unbounded was the previous
    behaviour, and the reason five jobs were found eight days into runs
    scheduled for a single morning.

    Returns True when this call created the Job, False when a Job with the
    same name already exists (another delivery of the same attempt won the
    race). Any other Kubernetes failure raises.
    """

    from .helpers import build_unity_job_manifest

    _create_or_replace_offline_env_secret(
        core_api,
        job_name=job_name,
        run_key=run_key,
        offline_env=offline_env,
    )
    manifest = build_unity_job_manifest(
        job_name=job_name,
        namespace=SETTINGS.default_namespace,
        ttl_seconds_after_finished=SETTINGS.offline_task_job_ttl_seconds,
        active_deadline_seconds=_bounded_offline_runtime(max_runtime_seconds),
        unity_status="offline",
        priority_class_name="unity-idle",
        app_label="unity-task-execution",
        backoff_limit=OFFLINE_TASK_JOB_BACKOFF_LIMIT,
        termination_grace_period_seconds=OFFLINE_TASK_TERMINATION_GRACE_PERIOD_SECONDS,
        extra_labels={
            "assistant-id": _normalize_task_id_component(request.assistant_id)[:63],
            "task-id": str(request.task_id),
        },
        extra_annotations={
            "unify.ai/task-execution-key": run_key,
            "unify.ai/source-task-log-id": str(request.source_task_log_id),
        },
    )
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [{"secretRef": {"name": job_name}}]
    try:
        job = batch_api.create_namespaced_job(
            namespace=SETTINGS.default_namespace,
            body=manifest,
        )
    except ApiException as exc:
        if exc.status != 409:
            raise
        existing = batch_api.read_namespaced_job(
            name=job_name,
            namespace=SETTINGS.default_namespace,
        )
        if isinstance(existing, dict):
            annotations = (existing.get("metadata") or {}).get("annotations") or {}
        else:
            annotations = (
                getattr(getattr(existing, "metadata", None), "annotations", None) or {}
            )
        existing_run_key = annotations.get("unify.ai/task-execution-key")
        if existing_run_key != run_key:
            raise ProviderEventDispatchValidationError(
                "offline_job_run_key_mismatch",
            )
        return False
    _adopt_offline_env_secret(core_api, job_name=job_name, job=job)
    return True


async def _assistant_desktop_browser_env(
    assistant_id: str,
    *,
    assistant_data: dict[str, Any],
) -> dict[str, str]:
    """Resolve a ready assistant desktop into browser-target runner variables.

    Scheduler workers remain the execution surface. This binding only makes
    website-facing browser work use the assistant's current desktop VM. When
    Computer Use is disabled, callers receive no desktop variables and retain
    normal worker-local browser behavior. Once entitled, a missing or draining
    VM is retryable; callers must never silently fall back.
    """

    if not managed_desktop_entitled(assistant_data):
        return {}
    custom_api = await asyncio.to_thread(get_custom_objects_api)
    if custom_api is None:
        raise HTTPException(
            status_code=503,
            detail="Assistant desktop browser target is temporarily unavailable",
        )
    session = await asyncio.to_thread(
        get_assistant_session,
        custom_api,
        SETTINGS.default_namespace,
        assistant_id,
    )
    if (
        session is None
        or assistant_session_desired_state(session) != DESIRED_STATE_RUNNING
    ):
        raise HTTPException(
            status_code=503,
            detail="Assistant desktop is not running; desktop-targeted task will retry",
        )
    conditions = (session.get("status") or {}).get("conditions") or []
    desktop_ready = any(
        condition.get("type") == "DesktopReady" and condition.get("status") == "True"
        for condition in conditions
        if isinstance(condition, dict)
    )
    binding = session_binding(session)
    desktop_url = binding_desktop_url(binding).strip()
    if not desktop_ready or not desktop_url.startswith("https://"):
        raise HTTPException(
            status_code=503,
            detail="Assistant desktop is not ready; desktop-targeted task will retry",
        )
    return {
        "ASSISTANT_BROWSER_TARGET": "assistant_desktop",
        "ASSISTANT_DESKTOP_URL": desktop_url,
        "ASSISTANT_ID": assistant_id,
    }


def _build_offline_runner_env(
    *,
    request: OfflineTaskDispatchRequest,
    execution: dict[str, Any],
    assistant_data: dict[str, Any],
    run_key: str,
    job_name: str,
    provider_event_dispatch: ProviderEventDispatchRequest | None = None,
) -> dict[str, str]:
    """Build environment variables for the disconnected Unity offline runner.

    Composes two layers:

    1. The task-specific UNITY_OFFLINE_TASK_* + ASSISTANT_ID vars from
       Unity's shared
       :func:`unify.task_scheduler.offline_runner_contract.build_offline_runner_env`
       — same source of truth the local in-process dispatcher uses.

    2. Hosted-only assistant-identity vars (UNIFY_KEY, ASSISTANT_*, USER_*,
       VOICE_*, TEAM_IDS, ORG_ID). Local / warm in-pod subprocesses inherit
       these from the parent process; cold AssistantSession wakes embed them
       in the bootstrap secret / offline env file.
    """

    entrypoint = (
        execution.get("entrypoint")
        if execution.get("entrypoint") is not None
        else request.entrypoint
    )
    team_ids = assistant_data.get("team_ids") or []
    team_summaries = assistant_data.get("team_summaries") or []
    self_contact_id = _required_contact_id(assistant_data, "self_contact_id")
    boss_contact_id = _required_contact_id(assistant_data, "boss_contact_id")
    requires_filesystem, requires_computer = _resolve_resource_flags(
        execution,
        request_requires_filesystem=request.requires_filesystem,
        request_requires_computer=request.requires_computer,
    )
    # Layer 1 — shared task-specific env (single source of truth in Unity).
    provider_event_kwargs: dict[str, Any] = {}
    if provider_event_dispatch is not None:
        issued_at = provider_event_dispatch.issued_at
        if issued_at.tzinfo is None:
            issued_at = issued_at.replace(tzinfo=timezone.utc)
        provider_event_kwargs = {
            "provider_event_operation_id": provider_event_dispatch.operation_id,
            "provider_event_run_id": provider_event_dispatch.run_id,
            "provider_event_binding_id": provider_event_dispatch.binding_id,
            "provider_event_receipt_id": provider_event_dispatch.receipt_id,
            "provider_event_context_ref": provider_event_dispatch.event_context_ref,
            "provider_event_issued_at": issued_at.astimezone(timezone.utc).isoformat(),
        }
    shared_kwargs: dict[str, Any] = {
        "assistant_id": (
            str(assistant_data.get("assistant_id") or request.assistant_id)
        ),
        "task_id": request.task_id,
        "source_task_log_id": request.source_task_log_id,
        "revision": request.revision,
        "wake": str(request.wake),
        "run_key": run_key,
        "task_name": str(execution.get("task_name") or ""),
        "scheduled_for": request.scheduled_for,
        "source_ref": request.source_ref,
        "source_medium": (
            request.source_medium or str(execution.get("trigger_medium") or "")
        ),
        "source_contact_id": request.source_contact_id,
        "entrypoint": entrypoint,
        "job_name": job_name,
        "requires_filesystem": requires_filesystem,
        "requires_computer": requires_computer,
        **provider_event_kwargs,
    }
    try:
        env = _build_offline_runner_env_shared(**shared_kwargs)
    except TypeError:
        # Unity contract may briefly lag the hosted kwargs; still emit the
        # resource-requirement env vars from this layer.
        shared_kwargs.pop("requires_filesystem", None)
        shared_kwargs.pop("requires_computer", None)
        env = _build_offline_runner_env_shared(**shared_kwargs)
        env["UNIFY_OFFLINE_TASK_REQUIRES_FILESYSTEM"] = (
            "1" if requires_filesystem else "0"
        )
        env["UNIFY_OFFLINE_TASK_REQUIRES_COMPUTER"] = "1" if requires_computer else "0"
    # Layer 2 — hosted-only assistant / user / voice identity, plus org and
    # transport vars the K8s job needs in env because there is no parent
    # process to inherit from. Local subprocesses skip this layer.
    env.update(
        {
            "UNIFY_KEY": str(assistant_data.get("api_key") or ""),
            "ASSISTANT_FIRST_NAME": str(
                assistant_data.get("assistant_first_name") or "",
            ),
            "ASSISTANT_SURNAME": str(assistant_data.get("assistant_surname") or ""),
            "ASSISTANT_AGE": str(assistant_data.get("assistant_age") or ""),
            "ASSISTANT_NATIONALITY": str(
                assistant_data.get("assistant_nationality") or "",
            ),
            "ASSISTANT_TIMEZONE": str(
                assistant_data.get("assistant_timezone") or "UTC",
            ),
            "ASSISTANT_ABOUT": str(assistant_data.get("assistant_about") or ""),
            "ASSISTANT_JOB_TITLE": str(
                assistant_data.get("assistant_job_title") or "",
            ),
            "ASSISTANT_NUMBER": str(assistant_data.get("assistant_number") or ""),
            "ASSISTANT_EMAIL": str(assistant_data.get("assistant_email") or ""),
            "ASSISTANT_WHATSAPP_NUMBER": str(
                assistant_data.get("assistant_whatsapp_number") or "",
            ),
            # Channel capability, not just channel address. A live session
            # learns which channels exist from its activation payload and from
            # inbound traffic; an offline run has neither, so omitting these
            # leaves every non-phone/email/WhatsApp channel looking disabled.
            # ASSISTANT_EMAIL_PROVIDER in particular gates Microsoft Teams:
            # defaulting it to google_workspace refuses every Teams send from
            # a task, whatever the assistant is really configured with.
            "ASSISTANT_EMAIL_PROVIDER": str(
                assistant_data.get("assistant_email_provider") or "google_workspace",
            ),
            "ASSISTANT_DISCORD_BOT_ID": str(
                assistant_data.get("assistant_discord_bot_id") or "",
            ),
            "ASSISTANT_SLACK_BOT_USER_ID": str(
                assistant_data.get("assistant_slack_bot_user_id") or "",
            ),
            "ASSISTANT_SLACK_TEAM_ID": str(
                assistant_data.get("assistant_slack_team_id") or "",
            ),
            "ASSISTANT_HAS_MS_TEAMS_BOT": (
                "true" if assistant_data.get("assistant_has_ms_teams_bot") else "false"
            ),
            "ASSISTANT_MS_TEAMS_TENANT_ID": str(
                assistant_data.get("assistant_ms_teams_tenant_id") or "",
            ),
            "SELF_CONTACT_ID": str(self_contact_id),
            "ASSISTANT_DESKTOP_MODE": str(
                assistant_data.get("desktop_mode") or "ubuntu",
            ),
            "ASSISTANT_USER_DESKTOPS": json.dumps(
                assistant_data.get("user_desktops") or [],
            ),
            "USER_ID": str(assistant_data.get("user_id") or ""),
            "USER_FIRST_NAME": str(assistant_data.get("user_first_name") or ""),
            "USER_SURNAME": str(assistant_data.get("user_surname") or ""),
            "USER_NUMBER": str(assistant_data.get("user_number") or ""),
            "USER_EMAIL": str(assistant_data.get("user_email") or ""),
            "USER_WHATSAPP_NUMBER": str(
                assistant_data.get("user_whatsapp_number") or "",
            ),
            "BOSS_CONTACT_ID": str(boss_contact_id),
            "VOICE_PROVIDER": str(
                assistant_data.get("voice_provider") or "cartesia",
            ),
            "VOICE_ID": str(assistant_data.get("voice_id") or ""),
            "VOICE_MODE": "tts",
            "ASSISTANT_DEFAULT_MODEL": str(
                assistant_data.get("default_model") or "",
            ),
            "ASSISTANT_DEFAULT_REASONING_EFFORT": str(
                assistant_data.get("default_reasoning_effort") or "",
            ),
            "ASSISTANT_SLOW_BRAIN_MODEL": str(
                assistant_data.get("slow_brain_model") or "",
            ),
            "ASSISTANT_SLOW_BRAIN_REASONING_EFFORT": str(
                assistant_data.get("slow_brain_reasoning_effort") or "",
            ),
            "TEAM_IDS": encode_int_list_for_env(team_ids, field_name="team_ids"),
            "TEAM_SUMMARIES": encode_team_summaries_for_env(
                team_summaries,
                field_name="team_summaries",
            ),
            "ORG_ID": (
                str(assistant_data.get("org_id"))
                if assistant_data.get("org_id") is not None
                else ""
            ),
            # Team-owned assistants have no personal root: shared-scoped tables
            # (Data, Tasks, Contacts, …) must resolve to Teams/{owner}/… . The
            # runtime reads this via SESSION_DETAILS.owner_team_id; omitting it
            # silently routes the offline tick to the personal root.
            "OWNER_TEAM_ID": _required_owner_team_id_env(assistant_data),
        },
    )
    destination = request.destination or execution.get("destination")
    if destination is not None:
        env["TASK_DESTINATION"] = str(destination)
    return env


def _required_owner_team_id_env(assistant_data: dict) -> str:
    """Return OWNER_TEAM_ID env value; refuse invalid team-owned payloads."""

    raw = assistant_data.get("owner_team_id")
    if raw is None:
        return ""
    try:
        owner_team_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Assistant {assistant_data.get('agent_id') or assistant_data.get('assistant_id')!r} "
            f"has invalid owner_team_id={raw!r}",
        ) from exc
    if owner_team_id <= 0:
        raise RuntimeError(
            f"Assistant {assistant_data.get('agent_id') or assistant_data.get('assistant_id')!r} "
            f"has invalid owner_team_id={owner_team_id}",
        )
    return str(owner_team_id)


def _delete_previous_materialization(
    request: ScheduledTaskExecutionUpsertRequest,
) -> bool:
    """Delete the previous Cloud Task when the execution identity changed."""

    if request.previous_revision is None or request.previous_scheduled_for is None:
        return False

    previous_delivery = request.previous_delivery or "live"
    if (
        request.previous_revision == request.revision
        and request.previous_scheduled_for == request.scheduled_for
        and previous_delivery == request.delivery
    ):
        return False

    return _delete_scheduled_execution_task(
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        revision=request.previous_revision,
        scheduled_for=request.previous_scheduled_for,
        delivery=previous_delivery,
    )


def _build_offline_run_create_payload(
    request: OfflineTaskDispatchRequest,
    run_key: str,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the initial Orchestra payload for one offline task run row."""

    execution = execution or {}
    return {
        "run_key": run_key,
        "assistant_id": request.assistant_id,
        "task_id": request.task_id,
        "destination": request.destination,
        "source_task_log_id": request.source_task_log_id,
        "wake": str(request.wake),
        "delivery": "offline",
        "entrypoint": (
            execution.get("entrypoint")
            if execution.get("entrypoint") is not None
            else request.entrypoint
        ),
        "revision": request.revision,
        "scheduled_for": _request_scheduled_for_iso(request),
        "source_medium": request.source_medium or None,
        "source_ref": request.source_ref or None,
        "source_contact_id": (
            str(request.source_contact_id)
            if request.source_contact_id is not None
            else None
        ),
        "source_contact_display_name": _optional_display_text(
            request.source_contact_display_name,
        ),
        "task_name": _optional_display_text(request.task_name)
        or _optional_display_text(execution.get("task_name")),
        "state": "pending",
    }


async def _materialize_scheduled_task_execution(
    request: ScheduledTaskExecutionUpsertRequest,
) -> dict[str, Any]:
    """Create or repair one scheduled execution delivery task."""

    try:
        previous_deleted = await asyncio.to_thread(
            _delete_previous_materialization,
            request,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to remove previous execution materialization: {exc}",
        ) from exc

    try:
        result = await asyncio.to_thread(_upsert_scheduled_execution_task, request)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to materialize scheduled execution: {exc}",
        ) from exc

    return {
        "success": True,
        "status": result["action"],
        "queue": result["queue"],
        "task_name": result["task_name"],
        "scheduled_for": result["scheduled_for"],
        "scheduled_checkpoint_for": result["scheduled_checkpoint_for"],
        "target_url": result["target_url"],
        "delivery": request.delivery,
        "previous_deleted": previous_deleted,
    }


def _execution_materialization_diagnostic(
    *,
    assistant_id: str,
    task_id: int,
    source_task_log_id: int | None = None,
) -> dict[str, Any]:
    """Return execution, queue, target, Cloud Task, and latest-run diagnostics."""

    execution = _lookup_current_task_execution(
        assistant_id=assistant_id,
        task_id=task_id,
    )
    latest_run = _lookup_latest_task_run(
        assistant_id=assistant_id,
        task_id=task_id,
        source_task_log_id=source_task_log_id,
    )
    materialization: dict[str, Any] | None = None
    if execution is not None and execution.get("wake") == "scheduled":
        scheduled_for_raw = execution.get("scheduled_for")
        if scheduled_for_raw:
            scheduled_for = datetime.fromisoformat(
                str(scheduled_for_raw).replace("Z", "+00:00"),
            ).astimezone(timezone.utc)
            delivery = str(execution.get("delivery") or "live")
            revision = str(execution.get("revision") or "")
            queue_name, target_url, schedule_at = _scheduled_execution_target(
                ScheduledTaskExecutionUpsertRequest(
                    assistant_id=assistant_id,
                    task_id=task_id,
                    source_task_log_id=int(
                        execution.get("source_task_log_id") or source_task_log_id or 0,
                    ),
                    revision=revision,
                    scheduled_for=scheduled_for,
                    delivery=("offline" if delivery == "offline" else "live"),
                ),
            )
            materialization = {
                "queue_name": queue_name,
                "queue_path": _task_queue_path(queue_name),
                "target_url": target_url,
                "scheduled_for": scheduled_for.isoformat(),
                "scheduled_checkpoint_for": schedule_at.isoformat(),
                "task_name": _scheduled_execution_task_name(
                    assistant_id=assistant_id,
                    task_id=task_id,
                    revision=revision,
                    scheduled_for=scheduled_for,
                    delivery=delivery,
                    queue_name=queue_name,
                ),
            }
            materialization.update(
                _cloud_task_diagnostic(materialization["task_name"]),
            )
    diagnostic = {
        "success": True,
        "assistant_id": assistant_id,
        "task_id": task_id,
        "execution": execution,
        "materialization": materialization,
        "queues": _task_queue_diagnostics(),
        "latest_run": latest_run,
    }
    diagnostic["health"] = _execution_health(
        execution=execution,
        materialization=materialization,
        latest_run=latest_run,
    )
    return diagnostic


def _diagnostic_needs_offline_job_status(diagnostic: dict[str, Any]) -> bool:
    """Return whether diagnosis should verify the stored Kubernetes job reference."""

    latest_run = diagnostic.get("latest_run")
    if not isinstance(latest_run, dict):
        return False
    if str(latest_run.get("delivery") or "") != "offline":
        return False
    if str(latest_run.get("state") or "") not in {"pending", "running"}:
        return False
    return bool(latest_run.get("job_name"))


def _attach_offline_job_status_to_diagnostic(
    *,
    diagnostic: dict[str, Any],
    batch_api: Any,
) -> dict[str, Any]:
    """Attach Kubernetes job state and recompute health for an offline run."""

    latest_run = diagnostic.get("latest_run")
    if not isinstance(latest_run, dict):
        return diagnostic
    job_name = str(latest_run.get("job_name") or "")
    if not job_name:
        return diagnostic
    latest_run_job = _classify_offline_job_status(batch_api, job_name)
    diagnostic["latest_run_job"] = latest_run_job
    diagnostic["health"] = _execution_health(
        execution=diagnostic.get("execution"),
        materialization=diagnostic.get("materialization"),
        latest_run=latest_run,
        latest_run_job=latest_run_job,
    )
    if diagnostic["health"].get("status") == "stale_running_run":
        diagnostic["blocking_conditions"] = [
            {
                "type": "stale_running_run",
                "run_key": latest_run.get("run_key"),
                "job_name": job_name,
                "job_status": latest_run_job.get("status"),
            },
        ]
    return diagnostic


def _latest_run_matches_execution(
    *,
    latest_run: dict[str, Any] | None,
    execution: dict[str, Any],
) -> bool:
    if latest_run is None:
        return False
    if int(latest_run.get("source_task_log_id") or 0) != int(
        execution.get("source_task_log_id") or 0,
    ):
        return False
    if str(latest_run.get("revision") or "") != str(
        execution.get("revision") or "",
    ):
        return False
    return _normalize_datetime_string(str(latest_run.get("scheduled_for") or "")) == (
        _normalize_datetime_string(str(execution.get("scheduled_for") or ""))
    )


def _execution_health(
    *,
    execution: dict[str, Any] | None,
    materialization: dict[str, Any] | None,
    latest_run: dict[str, Any] | None,
    latest_run_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify whether one current execution is armed, fired, or repairable."""

    if execution is None:
        return {"status": "execution_missing", "repairable": False}
    if execution.get("wake") != "scheduled":
        return {"status": "not_scheduled", "repairable": False}
    scheduled_for_raw = execution.get("scheduled_for")
    if not scheduled_for_raw:
        return {"status": "scheduled_time_missing", "repairable": False}
    scheduled_for = datetime.fromisoformat(
        str(scheduled_for_raw).replace("Z", "+00:00"),
    ).astimezone(timezone.utc)
    cloud_task_status = (
        materialization.get("cloud_task_status") if materialization else None
    )
    is_future = scheduled_for > datetime.now(timezone.utc)
    if is_future:
        if cloud_task_status == "present":
            return {"status": "armed_future", "repairable": False}
        return {
            "status": "stale_missing_materialization",
            "repairable": True,
            "cloud_task_status": cloud_task_status,
        }
    if not _latest_run_matches_execution(
        latest_run=latest_run,
        execution=execution,
    ):
        return {"status": "fired_no_matching_run", "repairable": True}
    run_state = str(latest_run.get("state") or "")
    if run_state in {"pending", "running"}:
        job_status = str((latest_run_job or {}).get("status") or "")
        if job_status in {"missing", "completed", "failed", "inactive", "stale"}:
            return {
                "status": "stale_running_run",
                "repairable": True,
                "run_state": run_state,
                "job_status": job_status,
            }
        return {"status": "fired_inflight", "repairable": False}
    if run_state == "failed":
        return {"status": "fired_failed_retryable", "repairable": True}
    if run_state == "completed":
        return {"status": "completed_not_rearmed", "repairable": True}
    return {"status": "fired_unknown_run_state", "repairable": True}


def _scheduled_execution_upsert_request_from_execution(
    execution: dict[str, Any],
) -> ScheduledTaskExecutionUpsertRequest:
    requires_filesystem, requires_computer = _resolve_resource_flags(execution)
    return ScheduledTaskExecutionUpsertRequest(
        assistant_id=str(execution.get("assistant_id") or ""),
        task_id=int(execution.get("task_id") or 0),
        source_task_log_id=int(execution.get("source_task_log_id") or 0),
        revision=str(execution.get("revision") or ""),
        scheduled_for=datetime.fromisoformat(
            str(execution.get("scheduled_for")).replace("Z", "+00:00"),
        ),
        delivery=("offline" if execution.get("delivery") == "offline" else "live"),
        requires_filesystem=requires_filesystem,
        requires_computer=requires_computer,
        entrypoint=(
            int(execution["entrypoint"])
            if execution.get("entrypoint") is not None
            else None
        ),
        task_label=_optional_display_text(execution.get("task_name")),
        # Orchestra projects a bounded summary of the authored description;
        # falling back to the title tells a woken assistant nothing about
        # what the work is.
        task_summary=_optional_display_text(execution.get("task_summary"))
        or _optional_display_text(execution.get("task_name")),
        recurrence_hint="recurring" if execution.get("recurring") else "one_off",
    )


def _offline_dispatch_request_from_execution(
    execution: dict[str, Any],
) -> OfflineTaskDispatchRequest:
    requires_filesystem, requires_computer = _resolve_resource_flags(execution)
    return OfflineTaskDispatchRequest(
        assistant_id=str(execution.get("assistant_id") or ""),
        task_id=int(execution.get("task_id") or 0),
        source_task_log_id=int(execution.get("source_task_log_id") or 0),
        revision=str(execution.get("revision") or ""),
        delivery="offline",
        requires_filesystem=requires_filesystem,
        requires_computer=requires_computer,
        entrypoint=(
            int(execution["entrypoint"])
            if execution.get("entrypoint") is not None
            else None
        ),
        wake=Wake.scheduled,
        scheduled_for=datetime.fromisoformat(
            str(execution.get("scheduled_for")).replace("Z", "+00:00"),
        ),
        task_name=_optional_display_text(execution.get("task_name")),
    )


@router.post("/task-execution/upsert")
async def upsert_scheduled_task_execution(
    request: ScheduledTaskExecutionUpsertRequest,
):
    """Materialize one scheduled execution into Cloud Tasks."""

    return await _materialize_scheduled_task_execution(request)


@router.get("/task-execution/validate")
async def validate_task_execution_infra():
    """Report required Cloud Tasks queues and configured execution targets."""

    try:
        queues = await asyncio.to_thread(_task_queue_diagnostics)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to validate task execution queues: {exc}",
        ) from exc
    return {
        "success": True,
        "gcp_project_id": SETTINGS.gcp_project_id,
        "location": SETTINGS.task_due_queue_location,
        "queues": queues,
        "job_lifecycle_safeguards": _offline_job_lifecycle_safeguards(),
        "targets": {
            "live_due": f"{SETTINGS.adapters_url}{TASK_DUE_ENDPOINT_PATH}",
            "offline_dispatch": f"{SETTINGS.comms_url}{OFFLINE_TASK_DISPATCH_PATH}",
            "repair": f"{SETTINGS.comms_url}{TASK_EXECUTION_REPAIR_PATH}",
        },
    }


@router.post("/task-execution/diagnose")
async def diagnose_task_execution(request: TaskExecutionDiagnosticRequest):
    """Report execution materialization and latest run state for one task."""

    try:
        diagnostic = await asyncio.to_thread(
            _execution_materialization_diagnostic,
            assistant_id=request.assistant_id,
            task_id=request.task_id,
            source_task_log_id=request.source_task_log_id,
        )
        if _diagnostic_needs_offline_job_status(diagnostic):
            batch_api, _, _, _ = await _get_k8s_clients()
            diagnostic = await asyncio.to_thread(
                _attach_offline_job_status_to_diagnostic,
                diagnostic=diagnostic,
                batch_api=batch_api,
            )
        return diagnostic
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Task execution diagnosis failed while talking to Orchestra: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to diagnose task execution: {exc}",
        ) from exc


@router.post("/task-execution/reconcile-current")
async def reconcile_current_task_execution(request: TaskExecutionDiagnosticRequest):
    """Diagnose one current execution and run the deterministic repair if safe."""

    diagnostic = await diagnose_task_execution(request)
    health = diagnostic.get("health") or {}
    if not health.get("repairable"):
        return {
            "success": True,
            "status": "noop",
            "reason": str(health.get("status") or "not_repairable"),
            "diagnostic": diagnostic,
            "summary": _task_execution_health_summary([diagnostic]),
        }
    repair = await repair_current_task_execution(request)
    return {
        "success": True,
        "status": "reconciled",
        "diagnostic": diagnostic,
        "repair": repair,
        "summary": _task_execution_health_summary([diagnostic]),
    }


@router.post("/task-execution/health")
async def task_execution_health(request: TaskExecutionDiagnosticRequest):
    """Return alertable health counts for one source-aware execution diagnostic."""

    diagnostic = await diagnose_task_execution(request)
    return {
        "success": True,
        "diagnostics": [diagnostic],
        "summary": _task_execution_health_summary([diagnostic]),
    }


@router.post("/task-execution/repair-current")
async def repair_current_task_execution(request: TaskExecutionDiagnosticRequest):
    """Repair the currently armed execution when diagnosis marks it repairable."""

    diagnostic = await diagnose_task_execution(request)
    health = diagnostic.get("health") or {}
    execution = diagnostic.get("execution")
    if not isinstance(execution, dict):
        return {
            "success": True,
            "status": "noop",
            "reason": "execution_missing",
            "diagnostic": diagnostic,
        }
    health_status = str(health.get("status") or "")
    if health_status == "stale_missing_materialization":
        result = await _materialize_scheduled_task_execution(
            _scheduled_execution_upsert_request_from_execution(execution),
        )
        return {
            "success": True,
            "status": "rematerialized",
            "result": result,
            "diagnostic": diagnostic,
        }
    if health_status in {"fired_failed_retryable", "stale_running_run"}:
        if execution.get("delivery") != "offline":
            return {
                "success": True,
                "status": "noop",
                "reason": "live_failed_retry_not_supported",
                "diagnostic": diagnostic,
            }
        result = await dispatch_offline_task(
            _offline_dispatch_request_from_execution(execution),
        )
        return {
            "success": True,
            "status": "retry_dispatched",
            "result": result,
            "diagnostic": diagnostic,
        }
    if health_status == "fired_no_matching_run" and (
        execution.get("delivery") == "offline"
    ):
        result = await dispatch_offline_task(
            _offline_dispatch_request_from_execution(execution),
        )
        return {
            "success": True,
            "status": "retry_dispatched",
            "result": result,
            "diagnostic": diagnostic,
        }
    if health_status in {
        "completed_not_rearmed",
        "fired_no_matching_run",
        "fired_unknown_run_state",
    }:
        result = await asyncio.to_thread(
            _reproject_task_execution,
            assistant_id=str(execution.get("assistant_id") or request.assistant_id),
            task_id=int(execution.get("task_id") or request.task_id),
        )
        return {
            "success": True,
            "status": "reprojected",
            "result": result,
            "diagnostic": diagnostic,
        }
    return {
        "success": True,
        "status": "noop",
        "reason": health_status,
        "diagnostic": diagnostic,
    }


@router.post("/task-execution/repair")
async def repair_scheduled_task_execution(
    request: ScheduledTaskExecutionUpsertRequest,
):
    """Re-evaluate a far-future execution and move it to the next queue."""

    return await _materialize_scheduled_task_execution(request)


@router.post("/task-execution/delete")
async def delete_scheduled_task_execution(
    request: ScheduledTaskExecutionDeleteRequest,
):
    """Delete one previously materialized scheduled execution."""

    try:
        deleted = await asyncio.to_thread(
            _delete_scheduled_execution_task,
            assistant_id=request.assistant_id,
            task_id=request.task_id,
            revision=request.revision,
            scheduled_for=request.scheduled_for,
            delivery=request.delivery,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete scheduled execution: {exc}",
        ) from exc

    return {
        "success": True,
        "deleted": deleted,
        "task_name": _scheduled_execution_task_name(
            assistant_id=request.assistant_id,
            task_id=request.task_id,
            revision=request.revision,
            scheduled_for=request.scheduled_for,
            delivery=request.delivery,
        ),
    }


@router.post("/offline-task/job-terminal")
async def terminalize_offline_task_job(
    request: OfflineTaskJobTerminalRequest,
):
    """Mirror a terminal ``unity-task-execution`` Job onto Orchestra Tasks/Executions.

    Called by the job-watcher when a Job reaches Complete or Failed. Idempotent:
    already-terminal Runs are left alone, and release-active no-ops when the
    Tasks row is no longer ``active``.
    """

    try:
        return await asyncio.to_thread(
            _terminalize_offline_job_outcome,
            assistant_id=request.assistant_id,
            run_key=request.run_key,
            source_task_log_id=request.source_task_log_id,
            job_name=request.job_name,
            terminal_type=request.terminal_type,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "Failed to terminalize offline Job %s for assistant %s",
            request.job_name,
            request.assistant_id,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to terminalize offline Job: {exc}",
        ) from exc


def _validate_offline_dispatch_request(request: OfflineTaskDispatchRequest) -> None:
    """Reject malformed offline-dispatch requests before touching external systems."""

    if request.delivery != "offline":
        raise HTTPException(
            status_code=400,
            detail="Offline dispatch requires delivery=offline",
        )
    if request.wake is Wake.scheduled and request.scheduled_for is None:
        raise HTTPException(
            status_code=400,
            detail="Scheduled offline dispatch requires scheduled_for",
        )


@assistant_self_router.post("/task-execution/offline-dispatch")
async def dispatch_offline_task(
    request: OfflineTaskDispatchRequest,
    request_fastapi: Request,
):
    """Validate and launch one headless offline task execution attempt.

    Auth: platform admin key (control-plane) or the dispatching assistant's own
    UNIFY_KEY, self-scoped to ``request.assistant_id``.
    """

    await authorize_admin_or_assistant(
        request_fastapi,
        assistant_id=request.assistant_id,
    )

    _validate_offline_dispatch_request(request)

    from communication.infra.drain import _ensure_waiter, admission_blocked

    drain_intent = admission_blocked(request.assistant_id)
    if drain_intent is not None:
        # The waiter that advances a drain lives in one Cloud Run instance; if
        # that instance recycled, the durable intent would block admission
        # forever. Re-arming here makes each deferred dispatch the thing that
        # revives it, so the retry Cloud Tasks schedules can succeed.
        await _ensure_waiter(str(request.assistant_id))
        # 503 so Cloud Tasks retries after the drain clears / pod recycles.
        raise HTTPException(
            status_code=503,
            detail=(
                "Assistant drain/restart is in progress "
                f"(state={drain_intent.state}); offline dispatch deferred"
            ),
        )

    stage = "accepted"
    run_key: str | None = None
    job_name: str | None = None
    _emit_task_execution_event(
        "task_execution.offline_dispatch.accepted",
        **_offline_dispatch_event_fields(request, stage=stage),
    )
    try:
        stage = "execution_lookup"
        _emit_task_execution_event(
            "task_execution.offline_dispatch.stage",
            **_offline_dispatch_event_fields(request, stage=stage),
        )
        execution = await asyncio.to_thread(
            _resolve_offline_dispatch_execution,
            request,
        )
        stage = "execution_validate"
        stale_reason = _validate_current_offline_execution(request, execution)
        if stale_reason is not None:
            _emit_task_execution_event(
                "task_execution.offline_dispatch.skipped",
                **_offline_dispatch_event_fields(
                    request,
                    stage=stage,
                    stale_reason=stale_reason,
                    status="skipped",
                ),
            )
            return {
                "success": True,
                "status": "skipped",
                "reason": stale_reason,
            }
        assistant_data = await asyncio.to_thread(
            _get_assistant_data,
            request.assistant_id,
        )
        if not assistant_has_task_destination(assistant_data, request.destination):
            return {
                "success": True,
                "status": "skipped",
                "reason": "destination_membership_revoked",
            }

        run_key = _resolve_offline_dispatch_run_key(request, execution)
        batch_api, core_api, _, _ = await _get_k8s_clients()

        stage = "overlap_check"
        skipped_overlap = await asyncio.to_thread(
            _skip_overlapped_scheduled_occurrence,
            batch_api=batch_api,
            request=request,
            run_key=run_key,
            execution=execution,
        )
        if skipped_overlap is not None:
            _emit_task_execution_event(
                "task_execution.offline_dispatch.skipped",
                **_offline_dispatch_event_fields(
                    request,
                    stage=stage,
                    run_key=run_key,
                    status="skipped_overlap",
                ),
            )
            return skipped_overlap

        stage = "run_create_or_adopt"
        _emit_task_execution_event(
            "task_execution.offline_dispatch.stage",
            **_offline_dispatch_event_fields(request, stage=stage, run_key=run_key),
        )
        run_response = await asyncio.to_thread(
            _create_or_adopt_task_run,
            _build_offline_run_create_payload(request, run_key, execution),
        )
        run = run_response.get("run") or {}
        created = bool(run_response.get("created"))
        run_state = str(run.get("state") or "pending")
        retry_count: int | None = None
        if not created and run_state == "completed":
            _emit_task_execution_event(
                "task_execution.offline_dispatch.adopted",
                **_offline_dispatch_event_fields(
                    request,
                    stage=stage,
                    run_key=run_key,
                    run_state=run_state,
                    status="adopted_terminal_run",
                ),
            )
            return {
                "success": True,
                "status": "adopted_terminal_run",
                "run_key": run_key,
                "run_state": run_state,
            }
        if not created and run_state == "failed":
            retry_count = int(run.get("retry_count") or 0) + 1
            await asyncio.to_thread(
                _release_active_task_source,
                assistant_id=request.assistant_id,
                source_task_log_id=request.source_task_log_id,
                mode="reopen",
                info=(
                    "Reopening Tasks source before offline retry of failed run "
                    f"{run_key}."
                ),
            )
            _emit_task_execution_event(
                "task_execution.offline_dispatch.retrying",
                **_offline_dispatch_event_fields(
                    request,
                    stage=stage,
                    run_key=run_key,
                    run_state=run_state,
                    status="retrying_failed_run",
                ),
            )
        if not created and run_state in {"pending", "running"} and run.get("job_name"):
            job_name = str(run.get("job_name"))
            job_status = await asyncio.to_thread(
                _classify_offline_job_status,
                batch_api,
                job_name,
            )
            if job_status.get("status") == "unknown":
                raise HTTPException(
                    status_code=503,
                    detail={
                        "status": "inflight_job_unknown",
                        "run_key": run_key,
                        "job_name": job_name,
                        "run_state": run_state,
                        "job_status": job_status,
                    },
                )
            if job_status.get("status") == "active":
                _emit_task_execution_event(
                    "task_execution.offline_dispatch.adopted",
                    **_offline_dispatch_event_fields(
                        request,
                        stage=stage,
                        run_key=run_key,
                        job_name=job_name,
                        run_state=run_state,
                        status="adopted_inflight_run",
                    ),
                )
                return {
                    "success": True,
                    "status": "adopted_inflight_run",
                    "run_key": run_key,
                    "job_name": job_name,
                    "run_state": run_state,
                    "job_status": job_status,
                }
            # The recorded Job is gone or terminal while the run row still
            # claims to be in flight: fail the stale row, reopen the Tasks
            # source if it is still active, and launch a retry.
            retry_count = int(run.get("retry_count") or 0) + 1
            await asyncio.to_thread(
                _fail_stale_inflight_run_and_reopen_source,
                assistant_id=request.assistant_id,
                source_task_log_id=request.source_task_log_id,
                run_key=run_key,
                run_state=run_state,
                job_status=job_status,
                retry_count=retry_count,
            )
            _emit_task_execution_event(
                "task_execution.offline_dispatch.retrying",
                **_offline_dispatch_event_fields(
                    request,
                    stage=stage,
                    run_key=run_key,
                    job_name=job_name,
                    run_state=run_state,
                    status="retrying_stale_inflight_run",
                ),
            )

        stage = "source_single_flight"
        conflict = await asyncio.to_thread(
            _adopt_inflight_source_conflict,
            batch_api=batch_api,
            request=request,
            exclude_run_key=run_key,
        )
        if conflict is not None:
            _emit_task_execution_event(
                "task_execution.offline_dispatch.adopted",
                **_offline_dispatch_event_fields(
                    request,
                    stage=stage,
                    run_key=str(conflict.get("run_key") or ""),
                    job_name=str(conflict.get("job_name") or ""),
                    run_state=str(conflict.get("run_state") or ""),
                    status="adopted_inflight_source",
                ),
            )
            return conflict

        stage = "launch_job"
        job_name = _build_offline_task_job_name(run_key, retry_count=retry_count)
        requires_filesystem, requires_computer = _resolve_resource_flags(
            execution or {},
            request_requires_filesystem=request.requires_filesystem,
            request_requires_computer=request.requires_computer,
        )
        desktop_browser_env: dict[str, str] = {}
        if requires_computer or requires_filesystem:
            stage = "desktop_target_resolve"
            desktop_browser_env = await _assistant_desktop_browser_env(
                request.assistant_id,
                assistant_data=assistant_data,
            )
        offline_env = _build_offline_runner_env(
            request=request,
            execution=execution or {},
            assistant_data=assistant_data,
            run_key=run_key,
            job_name=job_name,
        )
        offline_env.update(desktop_browser_env)
        _emit_task_execution_event(
            "task_execution.offline_dispatch.stage",
            **_offline_dispatch_event_fields(
                request,
                stage=stage,
                run_key=run_key,
                job_name=job_name,
            ),
        )
        max_runtime_raw = (execution or {}).get(
            "max_runtime_seconds",
        ) or request.max_runtime_seconds
        max_runtime_seconds = int(max_runtime_raw) if max_runtime_raw else None
        job_created = await asyncio.to_thread(
            _launch_offline_task_job,
            batch_api=batch_api,
            core_api=core_api,
            request=request,
            run_key=run_key,
            job_name=job_name,
            offline_env=offline_env,
            max_runtime_seconds=max_runtime_seconds,
        )
        if not job_created:
            _emit_task_execution_event(
                "task_execution.offline_dispatch.adopted",
                **_offline_dispatch_event_fields(
                    request,
                    stage=stage,
                    run_key=run_key,
                    job_name=job_name,
                    status="already_dispatched",
                ),
            )
            return {
                "success": True,
                "status": "already_dispatched",
                "run_key": run_key,
                "job_name": job_name,
            }
        await asyncio.to_thread(
            _update_task_run,
            assistant_id=request.assistant_id,
            run_key=run_key,
            updates=_running_task_run_updates(
                job_name,
                retry_count=retry_count,
            ),
        )
        _emit_task_execution_event(
            "task_execution.offline_dispatch.launched",
            **_offline_dispatch_event_fields(
                request,
                stage=stage,
                run_key=run_key,
                job_name=job_name,
                status="launched_job",
            ),
        )
        return {
            "success": True,
            "status": "launched_job",
            "run_key": run_key,
            "job_name": job_name,
        }
    except HTTPException as exc:
        _emit_task_execution_event(
            "task_execution.offline_dispatch.deferred",
            **_offline_dispatch_event_fields(
                request,
                stage=stage,
                run_key=run_key,
                job_name=job_name,
                error=exc,
            ),
        )
        raise
    except requests.RequestException as exc:
        _emit_task_execution_event(
            "task_execution.offline_dispatch.failed",
            **_offline_dispatch_event_fields(
                request,
                stage=stage,
                run_key=run_key,
                job_name=job_name,
                error=exc,
            ),
        )
        logger.exception("Offline dispatch failed while talking to Orchestra")
        raise HTTPException(
            status_code=502,
            detail=f"Offline dispatch failed while talking to Orchestra: {exc}",
        ) from exc
    except Exception as exc:
        _emit_task_execution_event(
            "task_execution.offline_dispatch.failed",
            **_offline_dispatch_event_fields(
                request,
                stage=stage,
                run_key=run_key,
                job_name=job_name,
                error=exc,
            ),
        )
        logger.exception("Failed to dispatch offline task")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to dispatch offline task: {exc}",
        ) from exc


@router.post(PROVIDER_EVENT_DISPATCH_PATH)
async def dispatch_provider_event_offline_route(
    request: ProviderEventDispatchRequest,
):
    """Adopt one pre-created provider-event run and launch at most one offline job.

    Auth: platform admin key (Orchestra trigger worker and control-plane callers).
    Launch ownership is claimed through Orchestra before Kubernetes I/O.
    """

    try:
        validate_provider_event_dispatch_request(
            request,
            ttl_seconds=SETTINGS.provider_event_dispatch_request_ttl_seconds,
        )
    except ProviderEventDispatchValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": exc.reason_code},
        ) from exc

    try:
        await asyncio.to_thread(_verify_precreated_provider_event_run, request)
    except ProviderEventDispatchValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": exc.reason_code},
        ) from exc

    batch_api, core_api, _, _ = await _get_k8s_clients()
    execution = await asyncio.to_thread(_provider_event_execution_metadata, request)
    assistant_data = await asyncio.to_thread(_get_assistant_data, request.assistant_id)
    requires_filesystem, requires_computer = _resolve_resource_flags(execution)
    desktop_browser_env: dict[str, str] = {}
    if requires_computer or requires_filesystem:
        desktop_browser_env = await _assistant_desktop_browser_env(
            request.assistant_id,
            assistant_data=assistant_data,
        )

    def launch_job(provider_request: ProviderEventDispatchRequest) -> str:
        offline_request = _offline_dispatch_request_from_provider_event(
            provider_request,
            execution=execution,
        )
        if not assistant_has_task_destination(
            assistant_data,
            offline_request.destination,
        ):
            raise ProviderEventDispatchValidationError(
                "destination_membership_revoked",
            )
        run_key = provider_request.run_key
        job_name = _build_offline_task_job_name(run_key)
        offline_env = _build_offline_runner_env(
            request=offline_request,
            execution=execution,
            assistant_data=assistant_data,
            run_key=run_key,
            job_name=job_name,
            provider_event_dispatch=provider_request,
        )
        offline_env.update(desktop_browser_env)
        _launch_offline_task_job(
            batch_api=batch_api,
            core_api=core_api,
            request=offline_request,
            run_key=run_key,
            job_name=job_name,
            offline_env=offline_env,
            max_runtime_seconds=None,
        )
        _update_task_run(
            assistant_id=provider_request.assistant_id,
            run_key=run_key,
            updates=_running_task_run_updates(job_name),
        )
        return job_name

    try:
        outcome = await asyncio.to_thread(
            _execute_provider_event_offline_dispatch,
            request,
            launch_job=launch_job,
        )
    except ProviderEventDispatchValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": exc.reason_code},
        ) from exc
    except ProviderEventDispatchAuthorizationError as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": exc.reason_code},
        ) from exc
    except requests.RequestException as exc:
        logger.exception(
            "Provider-event dispatch failed while talking to Orchestra",
        )
        raise HTTPException(
            status_code=502,
            detail=f"Provider-event dispatch failed while talking to Orchestra: {exc}",
        ) from exc

    return {
        "success": True,
        "operation_id": outcome.operation_id,
        "run_id": outcome.run_id,
        "run_key": outcome.run_key,
        "status": outcome.status,
        "job_name": outcome.job_name,
        "fencing_token": outcome.fencing_token,
        "adopted_only": outcome.adopted_only,
    }
