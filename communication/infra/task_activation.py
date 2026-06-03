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
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from fastapi import APIRouter, HTTPException
from google.api_core.exceptions import (
    AlreadyExists,
    NotFound as GcpNotFound,
    PermissionDenied as GcpPermissionDenied,
)
from google.protobuf import duration_pb2, timestamp_pb2

from common.assistant_lookup import get_assistant
from common.int_list_codec import encode_int_list_for_env
from common.space_summaries_codec import encode_space_summaries_for_env
from common.settings import SETTINGS
from common.task_destination import assistant_has_task_destination

# Single source of truth for the offline-runner subprocess contract.
# Imported from Unity so the hosted K8s job and the local in-process
# subprocess produce identical env-var dicts and run-keys for the same
# attempt. See unity.task_scheduler.offline_runner_contract for details.
from unity.task_scheduler.offline_runner_contract import (
    build_offline_run_key as _build_offline_run_key_shared,
    build_offline_runner_env as _build_offline_runner_env_shared,
)

from .helpers import create_unity_job
from .models import (
    OfflineTaskDispatchRequest,
    ScheduledTaskActivationDeleteRequest,
    ScheduledTaskActivationUpsertRequest,
    TaskActivationDiagnosticRequest,
)
from .runtime_clients import (
    get_cloud_tasks_client as _get_cloud_tasks_client,
    get_k8s_clients as _get_k8s_clients,
)

router = APIRouter()
logger = logging.getLogger(__name__)

TASK_DUE_ENDPOINT_PATH = "/scheduled/tasks/due"
TASK_ACTIVATION_REPAIR_PATH = "/infra/task-activation/repair"
OFFLINE_TASK_DISPATCH_PATH = "/infra/task-activation/offline-dispatch"
TASK_DUE_HTTP_TIMEOUT_SECONDS = 30
OFFLINE_UNITY_APP_LABEL = "unity-offline"
OFFLINE_UNITY_JOB_STATUS = "offline"
ORCHESTRA_TASK_MACHINE_PROJECT = "Assistants"
ORCHESTRA_TASK_ACTIVATION_CURRENT_PATH = "/admin/task-activation/current"
ORCHESTRA_TASK_ACTIVATION_REPROJECT_PATH = "/admin/task-activation/reproject"
ORCHESTRA_TASK_RUN_CREATE_OR_ADOPT_PATH = "/admin/task-run/create-or-adopt"
ORCHESTRA_TASK_RUN_LATEST_PATH = "/admin/task-run/latest"
ORCHESTRA_TASK_RUN_UPDATE_PATH = "/admin/task-run/update"
_TASK_ID_SAFE_RE = re.compile(r"[^a-z0-9-]+")
_task_queues_ensured: set[str] = set()


def _emit_task_activation_event(event: str, **fields: Any) -> None:
    logger.info(
        "OBS_EVENT %s",
        json.dumps(
            {"event": event, **fields},
            sort_keys=True,
            default=str,
        ),
    )


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
        "source_type": request.source_type,
        "execution_mode": request.execution_mode,
        "activation_revision": request.activation_revision,
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


def _task_queue_diagnostics() -> list[dict[str, Any]]:
    """Return existence diagnostics for required task activation queues."""

    client = _get_cloud_tasks_client()
    diagnostics: list[dict[str, Any]] = []
    for queue_name in dict.fromkeys(
        [
            SETTINGS.task_due_queue_name,
            SETTINGS.task_offline_queue_name,
            SETTINGS.task_activation_repair_queue_name,
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
        "entrypoint": request.entrypoint,
        "source_type": request.source_type,
        "task_label": request.task_label or "",
        "task_summary": request.task_summary or "",
        "visibility_policy": request.visibility_policy,
        "recurrence_hint": request.recurrence_hint,
    }
    if request.destination is not None:
        payload["destination"] = request.destination
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


def _require_cloud_task_present(task_name: str) -> None:
    """Fail materialization if the expected Cloud Task cannot be read back."""

    client = _get_cloud_tasks_client()
    try:
        client.get_task(name=task_name)
    except GcpNotFound as exc:
        raise RuntimeError(
            f"Cloud Task materialization missing after upsert: {task_name}",
        ) from exc


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


def _lookup_current_task_activation(
    *,
    assistant_id: str,
    task_id: int,
    destination: str | None = None,
) -> dict[str, Any] | None:
    """Fetch the current projected activation row for one assistant/task pair."""

    payload = {
        "project_name": ORCHESTRA_TASK_MACHINE_PROJECT,
        "assistant_id": assistant_id,
        "task_id": task_id,
    }
    if destination is not None:
        payload["destination"] = destination
    body = _orchestra_admin_post(
        ORCHESTRA_TASK_ACTIVATION_CURRENT_PATH,
        payload,
    )
    activation = body.get("activation")
    return activation if isinstance(activation, dict) else None


def _reproject_task_activation(
    *,
    assistant_id: str,
    task_id: int,
) -> dict[str, Any]:
    """Ask Orchestra to rebuild the current activation projection for one task."""

    return _orchestra_admin_post(
        ORCHESTRA_TASK_ACTIVATION_REPROJECT_PATH,
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
    body = _orchestra_admin_post(ORCHESTRA_TASK_RUN_LATEST_PATH, payload)
    run = body.get("run")
    return run if isinstance(run, dict) else None


def _get_assistant_data(assistant_id: str) -> dict[str, Any]:
    """Return assistant metadata from Orchestra for activation authorization."""

    assistant_data = get_assistant(assistant_id=assistant_id)
    if not assistant_data or not assistant_data.get("assistant_id"):
        raise RuntimeError(f"Assistant {assistant_id} no longer exists")
    return assistant_data


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


def _running_task_run_updates(
    job_name: str,
    *,
    retry_count: int | None = None,
    previous_error: str | None = None,
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
    if previous_error:
        updates["previous_error"] = previous_error
    return updates


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
    if activation.get("destination") != request.destination:
        return "destination_mismatch"
    if int(activation.get("source_task_log_id") or 0) != request.source_task_log_id:
        return "source_task_log_id_mismatch"
    activation_entrypoint = activation.get("entrypoint")
    if int(activation_entrypoint or 0) <= 0 and request.entrypoint:
        return "entrypoint_mismatch"
    if (
        activation_entrypoint
        and request.entrypoint
        and int(activation_entrypoint) != int(request.entrypoint)
    ):
        return "entrypoint_mismatch"
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
    """Build a stable idempotency key for one offline execution attempt.

    Thin adapter over the shared
    :func:`unity.task_scheduler.offline_runner_contract.build_offline_run_key`
    so the hosted K8s path and the local in-process path produce
    identical keys for the same attempt. If those keys ever diverged
    Orchestra's create-or-adopt path would fail to deduplicate
    concurrent attempts across topologies.

    Shared-space tasks insert a normalised destination segment between
    the assistant id and task id so concurrent attempts for different
    destinations never collide.
    """

    run_key = _build_offline_run_key_shared(
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        activation_revision=request.activation_revision,
        source_type=request.source_type,
        scheduled_for=request.scheduled_for,
        source_contact_id=request.source_contact_id,
        source_medium=request.source_medium,
        source_ref=request.source_ref,
    )
    if not request.destination:
        return run_key
    destination_part = f"{_normalize_task_id_component(request.destination)}:"
    prefix = f"offline:{request.source_type}:{request.assistant_id}:"
    if run_key.startswith(prefix):
        return f"{prefix}{destination_part}{run_key[len(prefix):]}"
    return run_key


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
    """Build environment variables for the headless Unity offline runner.

    Composes two layers:

    1. The task-specific UNITY_OFFLINE_TASK_* + ASSISTANT_ID vars from
       Unity's shared
       :func:`unity.task_scheduler.offline_runner_contract.build_offline_runner_env`
       — same source of truth the local in-process dispatcher uses. If
       this drifts, the hosted K8s job and the local subprocess would
       see different field shapes for the same task; the shared module
       prevents that by construction.

    2. Hosted-only assistant-identity vars (UNIFY_KEY, ASSISTANT_*, USER_*,
       VOICE_*, TEAM_IDS, ORG_ID). Local subprocesses inherit these from
       the parent conversation-manager's os.environ, so they live in
       Unity already; K8s jobs start in a fresh container and must
       receive them here.
    """

    entrypoint = activation.get("entrypoint") or request.entrypoint
    team_ids = assistant_data.get("team_ids") or []
    space_ids = assistant_data.get("space_ids") or []
    space_summaries = assistant_data.get("space_summaries") or []
    self_contact_id = _required_contact_id(assistant_data, "self_contact_id")
    boss_contact_id = _required_contact_id(assistant_data, "boss_contact_id")
    # Layer 1 — shared task-specific env (single source of truth in Unity).
    env = _build_offline_runner_env_shared(
        assistant_id=(str(assistant_data.get("assistant_id") or request.assistant_id)),
        task_id=request.task_id,
        source_task_log_id=request.source_task_log_id,
        activation_revision=request.activation_revision,
        source_type=request.source_type,
        run_key=run_key,
        task_name=str(activation.get("task_name") or ""),
        task_description=str(activation.get("task_description") or ""),
        scheduled_for=request.scheduled_for,
        source_ref=request.source_ref,
        source_medium=(
            request.source_medium or str(activation.get("trigger_medium") or "")
        ),
        source_contact_id=request.source_contact_id,
        entrypoint=entrypoint,
        job_name=job_name,
    )
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
            "SELF_CONTACT_ID": str(self_contact_id),
            "ASSISTANT_DESKTOP_MODE": "none",
            "ASSISTANT_USER_DESKTOP_MODE": "",
            "ASSISTANT_USER_DESKTOP_FILESYS_SYNC": "False",
            "ASSISTANT_USER_DESKTOP_URL": "",
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
            "TEAM_IDS": ",".join(str(team_id) for team_id in team_ids),
            "SPACE_IDS": encode_int_list_for_env(space_ids, field_name="space_ids"),
            "SPACE_SUMMARIES": encode_space_summaries_for_env(
                space_summaries,
                field_name="space_summaries",
            ),
            "ORG_ID": (
                str(assistant_data.get("org_id"))
                if assistant_data.get("org_id") is not None
                else ""
            ),
        },
    )
    destination = request.destination or activation.get("destination")
    if destination is not None:
        env["TASK_DESTINATION"] = str(destination)
    return env

def _launch_offline_task_job(
    *,
    batch_api: Any,
    request: OfflineTaskDispatchRequest,
    activation: dict[str, Any],
    assistant_data: dict[str, Any],
    run_key: str,
    job_name_seed: str | None = None,
) -> tuple[str, bool]:
    """Create the Kubernetes Job that runs the headless Unity executor."""

    job_name = _build_offline_job_name(job_name_seed or run_key)
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
        "destination": request.destination,
        "source_task_log_id": request.source_task_log_id,
        "source_type": request.source_type,
        "execution_mode": "offline",
        "entrypoint": activation.get("entrypoint") or request.entrypoint,
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


def _activation_materialization_diagnostic(
    *,
    assistant_id: str,
    task_id: int,
    source_task_log_id: int | None = None,
) -> dict[str, Any]:
    """Return activation, queue, target, Cloud Task, and latest-run diagnostics."""

    activation = _lookup_current_task_activation(
        assistant_id=assistant_id,
        task_id=task_id,
    )
    latest_run = _lookup_latest_task_run(
        assistant_id=assistant_id,
        task_id=task_id,
        source_task_log_id=source_task_log_id,
    )
    materialization: dict[str, Any] | None = None
    if activation is not None and activation.get("activation_kind") == "scheduled":
        scheduled_for_raw = activation.get("next_due_at")
        if scheduled_for_raw:
            scheduled_for = datetime.fromisoformat(
                str(scheduled_for_raw).replace("Z", "+00:00"),
            ).astimezone(timezone.utc)
            execution_mode = str(activation.get("execution_mode") or "live")
            activation_revision = str(activation.get("activation_revision") or "")
            queue_name, target_url, schedule_at = _scheduled_activation_target(
                ScheduledTaskActivationUpsertRequest(
                    assistant_id=assistant_id,
                    task_id=task_id,
                    source_task_log_id=int(
                        activation.get("source_task_log_id") or source_task_log_id or 0,
                    ),
                    activation_revision=activation_revision,
                    scheduled_for=scheduled_for,
                    execution_mode=(
                        "offline" if execution_mode == "offline" else "live"
                    ),
                ),
            )
            materialization = {
                "queue_name": queue_name,
                "queue_path": _task_queue_path(queue_name),
                "target_url": target_url,
                "scheduled_for": scheduled_for.isoformat(),
                "scheduled_checkpoint_for": schedule_at.isoformat(),
                "task_name": _scheduled_activation_task_name(
                    assistant_id=assistant_id,
                    task_id=task_id,
                    activation_revision=activation_revision,
                    scheduled_for=scheduled_for,
                    execution_mode=execution_mode,
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
        "activation": activation,
        "materialization": materialization,
        "queues": _task_queue_diagnostics(),
        "latest_run": latest_run,
    }
    diagnostic["health"] = _activation_health(
        activation=activation,
        materialization=materialization,
        latest_run=latest_run,
    )
    return diagnostic


def _latest_run_matches_activation(
    *,
    latest_run: dict[str, Any] | None,
    activation: dict[str, Any],
) -> bool:
    if latest_run is None:
        return False
    if int(latest_run.get("source_task_log_id") or 0) != int(
        activation.get("source_task_log_id") or 0,
    ):
        return False
    if str(latest_run.get("activation_revision") or "") != str(
        activation.get("activation_revision") or "",
    ):
        return False
    return _normalize_datetime_string(str(latest_run.get("scheduled_for") or "")) == (
        _normalize_datetime_string(str(activation.get("next_due_at") or ""))
    )


def _activation_health(
    *,
    activation: dict[str, Any] | None,
    materialization: dict[str, Any] | None,
    latest_run: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify whether one current activation is armed, fired, or repairable."""

    if activation is None:
        return {"status": "activation_missing", "repairable": False}
    if activation.get("activation_kind") != "scheduled":
        return {"status": "not_scheduled", "repairable": False}
    scheduled_for_raw = activation.get("next_due_at")
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
    if not _latest_run_matches_activation(
        latest_run=latest_run,
        activation=activation,
    ):
        return {"status": "fired_no_matching_run", "repairable": True}
    run_state = str(latest_run.get("state") or "")
    if run_state in {"pending", "running"}:
        return {"status": "fired_inflight", "repairable": False}
    if run_state == "failed":
        return {"status": "fired_failed_retryable", "repairable": True}
    if run_state == "completed":
        return {"status": "completed_not_rearmed", "repairable": True}
    return {"status": "fired_unknown_run_state", "repairable": True}


def _scheduled_activation_upsert_request_from_activation(
    activation: dict[str, Any],
) -> ScheduledTaskActivationUpsertRequest:
    return ScheduledTaskActivationUpsertRequest(
        assistant_id=str(activation.get("assistant_id") or ""),
        task_id=int(activation.get("task_id") or 0),
        source_task_log_id=int(activation.get("source_task_log_id") or 0),
        activation_revision=str(activation.get("activation_revision") or ""),
        scheduled_for=datetime.fromisoformat(
            str(activation.get("next_due_at")).replace("Z", "+00:00"),
        ),
        execution_mode=(
            "offline" if activation.get("execution_mode") == "offline" else "live"
        ),
        entrypoint=(
            int(activation["entrypoint"])
            if activation.get("entrypoint") is not None
            else None
        ),
        task_label=_optional_display_text(activation.get("task_name")),
        task_summary=_optional_display_text(activation.get("task_description")),
        recurrence_hint="recurring" if activation.get("repeat") else "one_off",
    )


def _offline_dispatch_request_from_activation(
    activation: dict[str, Any],
) -> OfflineTaskDispatchRequest:
    return OfflineTaskDispatchRequest(
        assistant_id=str(activation.get("assistant_id") or ""),
        task_id=int(activation.get("task_id") or 0),
        source_task_log_id=int(activation.get("source_task_log_id") or 0),
        activation_revision=str(activation.get("activation_revision") or ""),
        execution_mode="offline",
        entrypoint=(
            int(activation["entrypoint"])
            if activation.get("entrypoint") is not None
            else None
        ),
        source_type="scheduled",
        scheduled_for=datetime.fromisoformat(
            str(activation.get("next_due_at")).replace("Z", "+00:00"),
        ),
        task_name=_optional_display_text(activation.get("task_name")),
        task_description=_optional_display_text(activation.get("task_description")),
    )


@router.post("/task-activation/upsert")
async def upsert_scheduled_task_activation(
    request: ScheduledTaskActivationUpsertRequest,
):
    """Materialize one scheduled activation into Cloud Tasks."""

    return await _materialize_scheduled_task_activation(request)


@router.get("/task-activation/validate")
async def validate_task_activation_infra():
    """Report required Cloud Tasks queues and configured activation targets."""

    try:
        queues = await asyncio.to_thread(_task_queue_diagnostics)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to validate task activation queues: {exc}",
        ) from exc
    return {
        "success": True,
        "gcp_project_id": SETTINGS.gcp_project_id,
        "location": SETTINGS.task_due_queue_location,
        "queues": queues,
        "targets": {
            "live_due": f"{SETTINGS.adapters_url}{TASK_DUE_ENDPOINT_PATH}",
            "offline_dispatch": f"{SETTINGS.comms_url}{OFFLINE_TASK_DISPATCH_PATH}",
            "repair": f"{SETTINGS.comms_url}{TASK_ACTIVATION_REPAIR_PATH}",
        },
    }


@router.post("/task-activation/diagnose")
async def diagnose_task_activation(request: TaskActivationDiagnosticRequest):
    """Report activation materialization and latest run state for one task."""

    try:
        return await asyncio.to_thread(
            _activation_materialization_diagnostic,
            assistant_id=request.assistant_id,
            task_id=request.task_id,
            source_task_log_id=request.source_task_log_id,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Task activation diagnosis failed while talking to Orchestra: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to diagnose task activation: {exc}",
        ) from exc


@router.post("/task-activation/repair-current")
async def repair_current_task_activation(request: TaskActivationDiagnosticRequest):
    """Repair the currently armed activation when diagnosis marks it repairable."""

    diagnostic = await diagnose_task_activation(request)
    health = diagnostic.get("health") or {}
    activation = diagnostic.get("activation")
    if not isinstance(activation, dict):
        return {
            "success": True,
            "status": "noop",
            "reason": "activation_missing",
            "diagnostic": diagnostic,
        }
    health_status = str(health.get("status") or "")
    if health_status == "stale_missing_materialization":
        result = await _materialize_scheduled_task_activation(
            _scheduled_activation_upsert_request_from_activation(activation),
        )
        return {
            "success": True,
            "status": "rematerialized",
            "result": result,
            "diagnostic": diagnostic,
        }
    if health_status == "fired_failed_retryable":
        if activation.get("execution_mode") != "offline":
            return {
                "success": True,
                "status": "noop",
                "reason": "live_failed_retry_not_supported",
                "diagnostic": diagnostic,
            }
        result = await dispatch_offline_task(
            _offline_dispatch_request_from_activation(activation),
        )
        return {
            "success": True,
            "status": "retry_dispatched",
            "result": result,
            "diagnostic": diagnostic,
        }
    if health_status == "fired_no_matching_run" and (
        activation.get("execution_mode") == "offline"
    ):
        result = await dispatch_offline_task(
            _offline_dispatch_request_from_activation(activation),
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
            _reproject_task_activation,
            assistant_id=str(activation.get("assistant_id") or request.assistant_id),
            task_id=int(activation.get("task_id") or request.task_id),
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

    stage = "accepted"
    run_key: str | None = None
    job_name: str | None = None
    _emit_task_activation_event(
        "task_activation.offline_dispatch.accepted",
        **_offline_dispatch_event_fields(request, stage=stage),
    )
    try:
        stage = "activation_lookup"
        _emit_task_activation_event(
            "task_activation.offline_dispatch.stage",
            **_offline_dispatch_event_fields(request, stage=stage),
        )
        activation = await asyncio.to_thread(
            _lookup_current_task_activation,
            assistant_id=request.assistant_id,
            task_id=request.task_id,
            destination=request.destination,
        )
        stage = "activation_validate"
        stale_reason = _validate_current_offline_activation(request, activation)
        if stale_reason is not None:
            _emit_task_activation_event(
                "task_activation.offline_dispatch.skipped",
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

        run_key = _build_offline_run_key(request)
        stage = "run_create_or_adopt"
        _emit_task_activation_event(
            "task_activation.offline_dispatch.stage",
            **_offline_dispatch_event_fields(request, stage=stage, run_key=run_key),
        )
        run_response = await asyncio.to_thread(
            _create_or_adopt_task_run,
            _build_offline_run_create_payload(request, run_key, activation),
        )
        run = run_response.get("run") or {}
        created = bool(run_response.get("created"))
        run_state = str(run.get("state") or "pending")
        retry_count: int | None = None
        previous_error: str | None = None
        job_name_seed: str | None = None
        if not created and run_state == "completed":
            _emit_task_activation_event(
                "task_activation.offline_dispatch.adopted",
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
            previous_error = str(run.get("error") or "")
            job_name_seed = f"{run_key}:retry:{retry_count}"
            _emit_task_activation_event(
                "task_activation.offline_dispatch.retrying",
                **_offline_dispatch_event_fields(
                    request,
                    stage=stage,
                    run_key=run_key,
                    run_state=run_state,
                    status="retrying_failed_run",
                ),
            )
        if not created and run_state != "failed" and run.get("job_name"):
            job_name = str(run.get("job_name"))
            _emit_task_activation_event(
                "task_activation.offline_dispatch.adopted",
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
            }

        stage = "k8s_client"
        _emit_task_activation_event(
            "task_activation.offline_dispatch.stage",
            **_offline_dispatch_event_fields(request, stage=stage, run_key=run_key),
        )
        batch_api, _, _, _ = await _get_k8s_clients()
        stage = "job_launch"
        _emit_task_activation_event(
            "task_activation.offline_dispatch.stage",
            **_offline_dispatch_event_fields(request, stage=stage, run_key=run_key),
        )
        job_name, job_created = await asyncio.to_thread(
            _launch_offline_task_job,
            batch_api=batch_api,
            request=request,
            activation=activation or {},
            assistant_data=assistant_data,
            run_key=run_key,
            job_name_seed=job_name_seed,
        )
        stage = "run_mark_running"
        _emit_task_activation_event(
            "task_activation.offline_dispatch.stage",
            **_offline_dispatch_event_fields(
                request,
                stage=stage,
                run_key=run_key,
                job_name=job_name,
            ),
        )
        await asyncio.to_thread(
            _update_task_run,
            assistant_id=request.assistant_id,
            run_key=run_key,
            updates=_running_task_run_updates(
                job_name,
                retry_count=retry_count,
                previous_error=previous_error,
            ),
        )
        if not job_created:
            _emit_task_activation_event(
                "task_activation.offline_dispatch.adopted",
                **_offline_dispatch_event_fields(
                    request,
                    stage=stage,
                    run_key=run_key,
                    job_name=job_name,
                    status="job_already_exists",
                ),
            )
            return {
                "success": True,
                "status": "job_already_exists",
                "run_key": run_key,
                "job_name": job_name,
            }
    except requests.RequestException as exc:
        _emit_task_activation_event(
            "task_activation.offline_dispatch.failed",
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
        _emit_task_activation_event(
            "task_activation.offline_dispatch.failed",
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

    _emit_task_activation_event(
        "task_activation.offline_dispatch.launched",
        **_offline_dispatch_event_fields(
            request,
            stage=stage,
            run_key=run_key,
            job_name=job_name,
            status="launched",
        ),
    )
    return {
        "success": True,
        "status": "launched",
        "run_key": run_key,
        "job_name": job_name,
    }
