"""Scheduled and offline task-activation materialization for Communication.

This module owns the Cloud Tasks and Kubernetes job plumbing for the task
activation feature. It keeps the route handlers, queue naming, Orchestra admin
calls, and offline-run launch logic together so the rest of `infra/views.py`
can focus on assistant-session and VM lifecycle concerns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from fastapi import APIRouter, HTTPException
from google.api_core.exceptions import AlreadyExists, NotFound as GcpNotFound
from google.protobuf import duration_pb2, timestamp_pb2

from common.settings import SETTINGS

from .helpers import create_unity_job
from .models import (
    OfflineTaskDispatchRequest,
    ScheduledTaskActivationDeleteRequest,
    ScheduledTaskActivationUpsertRequest,
)
from .runtime_clients import (
    get_cloud_tasks_client as _get_cloud_tasks_client,
    get_k8s_clients as _get_k8s_clients,
)

router = APIRouter()

TASK_DUE_ENDPOINT_PATH = "/scheduled/tasks/due"
TASK_ACTIVATION_REPAIR_PATH = "/infra/task-activation/repair"
OFFLINE_TASK_DISPATCH_PATH = "/infra/task-activation/offline-dispatch"
TASK_DUE_HTTP_TIMEOUT_SECONDS = 30
OFFLINE_UNITY_APP_LABEL = "unity-offline"
OFFLINE_UNITY_JOB_STATUS = "offline"
ORCHESTRA_TASK_MACHINE_PROJECT = "Assistants"
ORCHESTRA_TASK_ACTIVATION_CURRENT_PATH = "/admin/task-activation/current"
ORCHESTRA_TASK_RUN_CREATE_OR_ADOPT_PATH = "/admin/task-run/create-or-adopt"
ORCHESTRA_TASK_RUN_UPDATE_PATH = "/admin/task-run/update"
_TASK_ID_SAFE_RE = re.compile(r"[^a-z0-9-]+")
_task_queues_ensured: set[str] = set()


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


def _scheduled_activation_task_name(
    *,
    assistant_id: str,
    task_id: int,
    activation_revision: str,
    scheduled_for: datetime,
    execution_mode: str = "live",
    queue_name: str | None = None,
) -> str:
    """Return the Cloud Tasks name for one scheduled activation delivery."""

    due_utc = scheduled_for.astimezone(timezone.utc)
    assistant_component = _normalize_task_id_component(str(assistant_id))[:32]
    revision_component = hashlib.sha256(
        activation_revision.encode("utf-8"),
    ).hexdigest()[:10]
    task_component = (
        f"task-{execution_mode}-{assistant_component}-{task_id}-"
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


def _scheduled_activation_http_body(
    request: ScheduledTaskActivationUpsertRequest,
) -> bytes:
    """Serialize the delayed task delivery payload."""

    payload = {
        "assistant_id": request.assistant_id,
        "task_id": request.task_id,
        "source_task_log_id": request.source_task_log_id,
        "activation_revision": request.activation_revision,
        "scheduled_for": request.scheduled_for.astimezone(timezone.utc).isoformat(),
        "execution_mode": request.execution_mode,
        "source_type": request.source_type,
        "task_label": request.task_label or "",
        "task_summary": request.task_summary or "",
        "visibility_policy": request.visibility_policy,
        "recurrence_hint": request.recurrence_hint,
    }
    return json.dumps(payload).encode("utf-8")


def _scheduled_activation_queue_candidates(execution_mode: str) -> list[str]:
    """Return every queue that may currently hold an activation task."""

    normalized_mode = "offline" if execution_mode == "offline" else "live"
    candidates = [
        SETTINGS.task_activation_repair_queue_name,
        (
            SETTINGS.task_due_queue_name
            if normalized_mode == "live"
            else SETTINGS.task_offline_queue_name
        ),
    ]
    return list(dict.fromkeys(candidates))


def _scheduled_activation_target(
    request: ScheduledTaskActivationUpsertRequest,
) -> tuple[str, str, datetime]:
    """Choose the queue, callback URL, and checkpoint time for this activation."""

    scheduled_for = request.scheduled_for.astimezone(timezone.utc)
    horizon_cutoff = datetime.now(timezone.utc) + timedelta(
        days=SETTINGS.task_activation_horizon_days,
    )
    if scheduled_for > horizon_cutoff:
        return (
            SETTINGS.task_activation_repair_queue_name,
            f"{SETTINGS.comms_url}{TASK_ACTIVATION_REPAIR_PATH}",
            horizon_cutoff,
        )
    if request.execution_mode == "offline":
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


def _delete_scheduled_activation_task(
    *,
    assistant_id: str,
    task_id: int,
    activation_revision: str,
    scheduled_for: datetime,
    execution_mode: str = "live",
) -> bool:
    """Delete one delayed activation task if it still exists."""

    deleted = False
    for queue_name in _scheduled_activation_queue_candidates(execution_mode):
        task_name = _scheduled_activation_task_name(
            assistant_id=assistant_id,
            task_id=task_id,
            activation_revision=activation_revision,
            scheduled_for=scheduled_for,
            execution_mode=execution_mode,
            queue_name=queue_name,
        )
        deleted = _delete_cloud_task_if_present(task_name) or deleted
    return deleted


def _upsert_scheduled_activation_task(
    request: ScheduledTaskActivationUpsertRequest,
) -> dict[str, Any]:
    """Create or repair the Cloud Task for one scheduled activation."""

    if not SETTINGS.orchestra_admin_key:
        raise RuntimeError("ORCHESTRA_ADMIN_KEY must be configured")
    if request.execution_mode == "live" and not SETTINGS.adapters_url:
        raise RuntimeError("UNITY_ADAPTERS_URL must be configured")
    if request.execution_mode == "offline" and not SETTINGS.comms_url:
        raise RuntimeError("COMMUNICATION_URL must be configured")

    from google.cloud import tasks_v2

    queue_name, target_url, schedule_at = _scheduled_activation_target(request)
    if target_url.startswith("/"):
        raise RuntimeError(
            "Target service URL must be configured for task activation materialization",
        )
    queue_path = _ensure_task_queue(queue_name)
    task_name = _scheduled_activation_task_name(
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        activation_revision=request.activation_revision,
        scheduled_for=request.scheduled_for,
        execution_mode=request.execution_mode,
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
            body=_scheduled_activation_http_body(request),
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
        _delete_cloud_task_if_present(task_name)
        try:
            client.create_task(parent=queue_path, task=task)
            action = "recreated"
        except AlreadyExists:
            action = "already_exists"

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


def _lookup_current_task_activation(
    *,
    assistant_id: str,
    task_id: int,
) -> dict[str, Any] | None:
    """Fetch the current projected activation row for one assistant/task pair."""

    body = _orchestra_admin_post(
        ORCHESTRA_TASK_ACTIVATION_CURRENT_PATH,
        {
            "project_name": ORCHESTRA_TASK_MACHINE_PROJECT,
            "assistant_id": assistant_id,
            "task_id": task_id,
        },
    )
    activation = body.get("activation")
    return activation if isinstance(activation, dict) else None


def _create_or_adopt_task_run(payload: dict[str, Any]) -> dict[str, Any]:
    """Create or adopt one offline task run row."""

    request_payload = {"project_name": ORCHESTRA_TASK_MACHINE_PROJECT, **payload}
    return _orchestra_admin_post(
        ORCHESTRA_TASK_RUN_CREATE_OR_ADOPT_PATH,
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
        ORCHESTRA_TASK_RUN_UPDATE_PATH,
        {
            "project_name": ORCHESTRA_TASK_MACHINE_PROJECT,
            "assistant_id": assistant_id,
            "run_key": run_key,
            "updates": updates,
        },
    )


def _running_task_run_updates(job_name: str) -> dict[str, str]:
    """Return the canonical Orchestra patch for one in-flight offline run."""

    return {
        "state": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "job_name": job_name,
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


def _validate_current_offline_activation(
    request: OfflineTaskDispatchRequest,
    activation: dict[str, Any] | None,
) -> str | None:
    """Return a stale reason when the dispatch no longer matches the current activation."""

    if activation is None:
        return "activation_missing"
    expected_kind = "scheduled" if request.source_type == "scheduled" else "triggered"
    if activation.get("activation_kind") != expected_kind:
        return "activation_kind_changed"
    if activation.get("execution_mode") != "offline":
        return "execution_mode_changed"
    if activation.get("activation_revision") != request.activation_revision:
        return "activation_revision_mismatch"
    if int(activation.get("source_task_log_id") or 0) != request.source_task_log_id:
        return "source_task_log_id_mismatch"
    if int(activation.get("entrypoint") or 0) <= 0:
        return "missing_entrypoint"
    if request.source_type == "scheduled" and _normalize_datetime_string(
        activation.get("next_due_at"),
    ) != _normalize_datetime_string(_request_scheduled_for_iso(request)):
        return "scheduled_for_mismatch"
    return None


def _request_scheduled_for_iso(request: OfflineTaskDispatchRequest) -> str | None:
    """Return the request's scheduled timestamp in canonical UTC ISO format."""

    if request.scheduled_for is None:
        return None
    return request.scheduled_for.astimezone(timezone.utc).isoformat()


def _build_offline_run_key(request: OfflineTaskDispatchRequest) -> str:
    """Build a stable idempotency key for one offline execution attempt."""

    revision_digest = hashlib.sha256(
        request.activation_revision.encode("utf-8"),
    ).hexdigest()[:12]
    tail_parts = []
    if request.scheduled_for is not None:
        tail_parts.append(
            request.scheduled_for.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        )
    if request.source_contact_id is not None:
        tail_parts.append(f"contact-{request.source_contact_id}")
    if request.source_medium:
        tail_parts.append(_normalize_task_id_component(request.source_medium)[:24])
    if request.source_ref:
        tail_parts.append(
            hashlib.sha256(request.source_ref.encode("utf-8")).hexdigest()[:12],
        )
    tail = "-".join(tail_parts) or "once"
    return (
        f"offline:{request.source_type}:{request.assistant_id}:"
        f"{request.task_id}:{revision_digest}:{tail}"
    )


def _build_offline_job_name(run_key: str) -> str:
    """Return the deterministic Kubernetes Job name for one offline run."""

    digest = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:12]
    base_name = f"unity-offline-{digest}"
    suffix = SETTINGS.env_suffix.lstrip("-")
    return f"{base_name}-{suffix}" if suffix else base_name


def _build_offline_runner_env(
    *,
    request: OfflineTaskDispatchRequest,
    activation: dict[str, Any],
    assistant_data: dict[str, Any],
    run_key: str,
    job_name: str,
) -> dict[str, str]:
    """Build environment variables for the headless Unity offline runner."""

    team_ids = assistant_data.get("team_ids") or []
    task_request = (
        str(activation.get("task_description") or "").strip()
        or str(activation.get("task_name") or "").strip()
        or f"Execute task {request.task_id}"
    )
    return {
        "UNITY_OFFLINE_TASK_MODE": "function",
        "EVENTBUS_PUBLISHING_ENABLED": "false",
        "EVENTBUS_PUBSUB_STREAMING": "false",
        "UNITY_OFFLINE_TASK_RUN_KEY": run_key,
        "UNITY_OFFLINE_TASK_JOB_NAME": job_name,
        "UNITY_OFFLINE_TASK_ID": str(request.task_id),
        "UNITY_OFFLINE_TASK_SOURCE_TASK_LOG_ID": str(request.source_task_log_id),
        "UNITY_OFFLINE_TASK_ACTIVATION_REVISION": request.activation_revision,
        "UNITY_OFFLINE_TASK_FUNCTION_ID": str(int(activation["entrypoint"])),
        "UNITY_OFFLINE_TASK_REQUEST": task_request,
        "UNITY_OFFLINE_TASK_NAME": str(activation.get("task_name") or ""),
        "UNITY_OFFLINE_TASK_DESCRIPTION": str(activation.get("task_description") or ""),
        "UNITY_OFFLINE_TASK_SOURCE_TYPE": request.source_type,
        "UNITY_OFFLINE_TASK_SCHEDULED_FOR": _request_scheduled_for_iso(request) or "",
        "UNITY_OFFLINE_TASK_SOURCE_REF": request.source_ref or "",
        "UNITY_OFFLINE_TASK_SOURCE_MEDIUM": (
            request.source_medium or str(activation.get("trigger_medium") or "")
        ),
        "UNITY_OFFLINE_TASK_SOURCE_CONTACT_ID": (
            str(request.source_contact_id)
            if request.source_contact_id is not None
            else ""
        ),
        "UNIFY_KEY": str(assistant_data.get("api_key") or ""),
        "ASSISTANT_ID": str(assistant_data.get("assistant_id") or request.assistant_id),
        "ASSISTANT_FIRST_NAME": str(assistant_data.get("assistant_first_name") or ""),
        "ASSISTANT_SURNAME": str(assistant_data.get("assistant_surname") or ""),
        "ASSISTANT_AGE": str(assistant_data.get("assistant_age") or ""),
        "ASSISTANT_NATIONALITY": str(assistant_data.get("assistant_nationality") or ""),
        "ASSISTANT_TIMEZONE": str(assistant_data.get("assistant_timezone") or "UTC"),
        "ASSISTANT_ABOUT": str(assistant_data.get("assistant_about") or ""),
        "ASSISTANT_JOB_TITLE": str(assistant_data.get("assistant_job_title") or ""),
        "ASSISTANT_NUMBER": str(assistant_data.get("assistant_number") or ""),
        "ASSISTANT_EMAIL": str(assistant_data.get("assistant_email") or ""),
        "ASSISTANT_WHATSAPP_NUMBER": str(
            assistant_data.get("assistant_whatsapp_number") or "",
        ),
        "ASSISTANT_DESKTOP_MODE": "none",
        "ASSISTANT_USER_DESKTOP_MODE": "",
        "ASSISTANT_USER_DESKTOP_FILESYS_SYNC": "False",
        "ASSISTANT_USER_DESKTOP_URL": "",
        "USER_ID": str(assistant_data.get("user_id") or ""),
        "USER_FIRST_NAME": str(assistant_data.get("user_first_name") or ""),
        "USER_SURNAME": str(assistant_data.get("user_surname") or ""),
        "USER_NUMBER": str(assistant_data.get("user_number") or ""),
        "USER_EMAIL": str(assistant_data.get("user_email") or ""),
        "USER_WHATSAPP_NUMBER": str(assistant_data.get("user_whatsapp_number") or ""),
        "VOICE_PROVIDER": str(assistant_data.get("voice_provider") or "cartesia"),
        "VOICE_ID": str(assistant_data.get("voice_id") or ""),
        "VOICE_MODE": "tts",
        "TEAM_IDS": ",".join(str(team_id) for team_id in team_ids),
        "ORG_ID": (
            str(assistant_data.get("org_id"))
            if assistant_data.get("org_id") is not None
            else ""
        ),
    }


def _get_assistant_data(assistant_id: str) -> dict[str, Any]:
    """Fetch one assistant payload through the adapters helper."""

    from adapters.helpers import get_assistant

    assistant_data = get_assistant(assistant_id=assistant_id)
    return assistant_data if isinstance(assistant_data, dict) else {}


def _launch_offline_task_job(
    *,
    batch_api: Any,
    request: OfflineTaskDispatchRequest,
    activation: dict[str, Any],
    run_key: str,
) -> tuple[str, bool]:
    """Create the Kubernetes Job that runs the headless Unity executor."""

    assistant_data = _get_assistant_data(request.assistant_id)
    if not assistant_data or not assistant_data.get("assistant_id"):
        raise RuntimeError(f"Assistant {request.assistant_id} no longer exists")

    job_name = _build_offline_job_name(run_key)
    job = create_unity_job(
        batch_api,
        job_name=job_name,
        namespace=SETTINGS.default_namespace,
        ttl_seconds_after_finished=SETTINGS.offline_task_job_ttl_seconds,
        active_deadline_seconds=SETTINGS.offline_task_job_active_deadline_seconds,
        unity_status=OFFLINE_UNITY_JOB_STATUS,
        priority_class_name="unity-idle",
        app_label=OFFLINE_UNITY_APP_LABEL,
        extra_labels={
            "assistant-id": _normalize_task_id_component(request.assistant_id)[:63],
            "unity-status": OFFLINE_UNITY_JOB_STATUS,
        },
        extra_annotations={
            "unify.ai/task-run-key": run_key,
            "unify.ai/task-id": str(request.task_id),
            "unify.ai/task-source-type": request.source_type,
        },
        extra_env=_build_offline_runner_env(
            request=request,
            activation=activation,
            assistant_data=assistant_data,
            run_key=run_key,
            job_name=job_name,
        ),
    )
    return job_name, job is not None


def _delete_previous_materialization(
    request: ScheduledTaskActivationUpsertRequest,
) -> bool:
    """Delete the previous Cloud Task when the activation identity changed."""

    if (
        request.previous_activation_revision is None
        or request.previous_scheduled_for is None
    ):
        return False

    previous_execution_mode = request.previous_execution_mode or "live"
    if (
        request.previous_activation_revision == request.activation_revision
        and request.previous_scheduled_for == request.scheduled_for
        and previous_execution_mode == request.execution_mode
    ):
        return False

    return _delete_scheduled_activation_task(
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        activation_revision=request.previous_activation_revision,
        scheduled_for=request.previous_scheduled_for,
        execution_mode=previous_execution_mode,
    )


def _build_offline_run_create_payload(
    request: OfflineTaskDispatchRequest,
    run_key: str,
    activation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the initial Orchestra payload for one offline task run row."""

    activation = activation or {}
    return {
        "run_key": run_key,
        "assistant_id": request.assistant_id,
        "task_id": request.task_id,
        "source_task_log_id": request.source_task_log_id,
        "source_type": request.source_type,
        "execution_mode": "offline",
        "activation_revision": request.activation_revision,
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
        or _optional_display_text(activation.get("task_name")),
        "task_description": _optional_display_text(request.task_description)
        or _optional_display_text(activation.get("task_description")),
        "state": "pending",
    }


async def _materialize_scheduled_task_activation(
    request: ScheduledTaskActivationUpsertRequest,
) -> dict[str, Any]:
    """Create or repair one scheduled activation delivery task."""

    try:
        previous_deleted = await asyncio.to_thread(
            _delete_previous_materialization,
            request,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to remove previous activation materialization: {exc}",
        ) from exc

    try:
        result = await asyncio.to_thread(_upsert_scheduled_activation_task, request)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to materialize scheduled activation: {exc}",
        ) from exc

    return {
        "success": True,
        "status": result["action"],
        "queue": result["queue"],
        "task_name": result["task_name"],
        "scheduled_for": result["scheduled_for"],
        "scheduled_checkpoint_for": result["scheduled_checkpoint_for"],
        "target_url": result["target_url"],
        "execution_mode": request.execution_mode,
        "previous_deleted": previous_deleted,
    }


@router.post("/task-activation/upsert")
async def upsert_scheduled_task_activation(
    request: ScheduledTaskActivationUpsertRequest,
):
    """Materialize one scheduled activation into Cloud Tasks."""

    return await _materialize_scheduled_task_activation(request)


@router.post("/task-activation/repair")
async def repair_scheduled_task_activation(
    request: ScheduledTaskActivationUpsertRequest,
):
    """Re-evaluate a far-future activation and move it to the next queue."""

    return await _materialize_scheduled_task_activation(request)


@router.post("/task-activation/delete")
async def delete_scheduled_task_activation(
    request: ScheduledTaskActivationDeleteRequest,
):
    """Delete one previously materialized scheduled activation."""

    try:
        deleted = await asyncio.to_thread(
            _delete_scheduled_activation_task,
            assistant_id=request.assistant_id,
            task_id=request.task_id,
            activation_revision=request.activation_revision,
            scheduled_for=request.scheduled_for,
            execution_mode=request.execution_mode,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete scheduled activation: {exc}",
        ) from exc

    return {
        "success": True,
        "deleted": deleted,
        "task_name": _scheduled_activation_task_name(
            assistant_id=request.assistant_id,
            task_id=request.task_id,
            activation_revision=request.activation_revision,
            scheduled_for=request.scheduled_for,
            execution_mode=request.execution_mode,
        ),
    }


def _validate_offline_dispatch_request(request: OfflineTaskDispatchRequest) -> None:
    """Reject malformed offline-dispatch requests before touching external systems."""

    if request.execution_mode != "offline":
        raise HTTPException(
            status_code=400,
            detail="Offline dispatch requires execution_mode=offline",
        )
    if request.source_type == "scheduled" and request.scheduled_for is None:
        raise HTTPException(
            status_code=400,
            detail="Scheduled offline dispatch requires scheduled_for",
        )


@router.post("/task-activation/offline-dispatch")
async def dispatch_offline_task(request: OfflineTaskDispatchRequest):
    """Validate and launch one headless offline task execution attempt."""

    _validate_offline_dispatch_request(request)

    try:
        activation = await asyncio.to_thread(
            _lookup_current_task_activation,
            assistant_id=request.assistant_id,
            task_id=request.task_id,
        )
        stale_reason = _validate_current_offline_activation(request, activation)
        if stale_reason is not None:
            return {
                "success": True,
                "status": "skipped",
                "reason": stale_reason,
            }

        run_key = _build_offline_run_key(request)
        run_response = await asyncio.to_thread(
            _create_or_adopt_task_run,
            _build_offline_run_create_payload(request, run_key, activation),
        )
        run = run_response.get("run") or {}
        created = bool(run_response.get("created"))
        run_state = str(run.get("state") or "pending")
        if not created and run_state in {"completed", "failed"}:
            return {
                "success": True,
                "status": "adopted_terminal_run",
                "run_key": run_key,
                "run_state": run_state,
            }
        if not created and run.get("job_name"):
            return {
                "success": True,
                "status": "adopted_inflight_run",
                "run_key": run_key,
                "job_name": run.get("job_name"),
                "run_state": run_state,
            }

        batch_api, _, _, _ = await _get_k8s_clients()
        job_name, job_created = await asyncio.to_thread(
            _launch_offline_task_job,
            batch_api=batch_api,
            request=request,
            activation=activation or {},
            run_key=run_key,
        )
        await asyncio.to_thread(
            _update_task_run,
            assistant_id=request.assistant_id,
            run_key=run_key,
            updates=_running_task_run_updates(job_name),
        )
        if not job_created:
            return {
                "success": True,
                "status": "job_already_exists",
                "run_key": run_key,
                "job_name": job_name,
            }
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Offline dispatch failed while talking to Orchestra: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to dispatch offline task: {exc}",
        ) from exc

    return {
        "success": True,
        "status": "launched",
        "run_key": run_key,
        "job_name": job_name,
    }
