"""Scheduled and offline task-activation materialization for Communication.

This module owns Cloud Tasks materialization, Orchestra admin calls, and
offline-run launches. An offline run is a plain one-shot Kubernetes Job
(``unity-task-run-*``) carrying the full runner env: no AssistantSession,
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

from common.assistant_lookup import get_assistant
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
from unify.task_scheduler.types.run_source import RunSource

from communication.dependencies import authorize_admin_or_assistant
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
from .self_router import assistant_self_router

router = APIRouter()
logger = logging.getLogger(__name__)

TASK_DUE_ENDPOINT_PATH = "/scheduled/tasks/due"
TASK_ACTIVATION_REPAIR_PATH = "/infra/task-activation/repair"
OFFLINE_TASK_DISPATCH_PATH = "/infra/task-activation/offline-dispatch"
TASK_DUE_HTTP_TIMEOUT_SECONDS = 30
ORCHESTRA_TASK_MACHINE_PROJECT = "Assistants"
ORCHESTRA_TASK_ACTIVATION_CURRENT_PATH = "/admin/task-activation/current"
ORCHESTRA_TASK_ACTIVATION_REPROJECT_PATH = "/admin/task-activation/reproject"
ORCHESTRA_TASK_RUN_CREATE_OR_ADOPT_PATH = "/admin/task-run/create-or-adopt"
ORCHESTRA_TASK_RUN_LATEST_PATH = "/admin/task-run/latest"
ORCHESTRA_TASK_RUN_UPDATE_PATH = "/admin/task-run/update"
OFFLINE_TASK_JOB_BACKOFF_LIMIT = 0
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
        raise RuntimeError("UNITY_COMMS_URL must be configured")

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


def _activation_snapshot_from_explicit_dispatch_request(
    request: OfflineTaskDispatchRequest,
) -> dict[str, Any]:
    """Build an activation-shaped snapshot from an explicit dispatch request.

    Explicit REST triggers are issued by Orchestra immediately after it resolves
    the current activation. Re-fetching ``/admin/task-activation/current`` is a
    redundant round-trip (and previously contended on the still-open resolve
    transaction). Trust the caller-supplied fields for launch + validation.
    """

    snapshot: dict[str, Any] = {
        "assistant_id": request.assistant_id,
        "task_id": request.task_id,
        "source_task_log_id": request.source_task_log_id,
        "activation_revision": request.activation_revision,
        "execution_mode": request.execution_mode,
        "destination": request.destination,
        "entrypoint": request.entrypoint,
        "max_runtime_seconds": request.max_runtime_seconds,
        "task_name": request.task_name,
        "task_description": request.task_description,
    }
    if request.scheduled_for is not None:
        snapshot["next_due_at"] = request.scheduled_for.astimezone(
            timezone.utc,
        ).isoformat()
    return snapshot


def _resolve_offline_dispatch_activation(
    request: OfflineTaskDispatchRequest,
) -> dict[str, Any] | None:
    """Return the activation used to validate and launch one offline dispatch."""

    if request.source_type == "explicit":
        return _activation_snapshot_from_explicit_dispatch_request(request)
    return _lookup_current_task_activation(
        assistant_id=request.assistant_id,
        task_id=request.task_id,
        destination=request.destination,
    )


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


def _failed_task_run_updates(
    *,
    error: str,
    result_summary: str,
    retry_count: int | None = None,
    previous_error: str | None = None,
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
    if previous_error:
        updates["previous_error"] = previous_error
    return updates


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
        "active_deadline_seconds": "per-task max_runtime_seconds (None = unbounded)",
        "ttl_seconds_after_finished": SETTINGS.offline_task_job_ttl_seconds,
        "backoff_limit": OFFLINE_TASK_JOB_BACKOFF_LIMIT,
        "durable_terminal_state": "Tasks/Runs and Tasks rows",
    }


def _task_activation_health_summary(
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return compact alertable counts for task activation diagnostics."""

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


def _validate_current_offline_activation(
    request: OfflineTaskDispatchRequest,
    activation: dict[str, Any] | None,
) -> str | None:
    """Return a stale reason when the dispatch no longer matches the current activation."""

    if activation is None:
        return "activation_missing"
    # Manual REST triggers may fire any offline activation (scheduled or
    # communication-triggered). Other dispatches still require kind match.
    source_type = RunSource.normalize(request.source_type)
    if source_type.requires_activation_kind_match:
        if activation.get("activation_kind") != source_type.activation_kind:
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
    if source_type is RunSource.scheduled and _normalize_datetime_string(
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
    base_name = f"unity-task-run-{digest}"
    suffix = SETTINGS.env_suffix.lstrip("-")
    return f"{base_name}-{suffix}" if suffix else base_name


def _create_or_replace_offline_env_secret(
    core_api: Any,
    *,
    job_name: str,
    run_key: str,
    offline_env: dict[str, str],
) -> None:
    """Write the per-run env Secret consumed by the task-run Job via envFrom.

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
            labels={"app": "unity-task-run"},
            annotations={"unify.ai/task-run-key": run_key},
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

    ``max_runtime_seconds`` is the task's own execution bound; ``None`` leaves
    the Job unbounded (long-running scrapes legitimately run for days).
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
        active_deadline_seconds=max_runtime_seconds,
        unity_status="offline",
        priority_class_name="unity-idle",
        app_label="unity-task-run",
        extra_labels={
            "assistant-id": _normalize_task_id_component(request.assistant_id)[:63],
            "task-id": str(request.task_id),
        },
        extra_annotations={
            "unify.ai/task-run-key": run_key,
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
        if exc.status == 409:
            return False
        raise
    _adopt_offline_env_secret(core_api, job_name=job_name, job=job)
    return True


def _build_offline_runner_env(
    *,
    request: OfflineTaskDispatchRequest,
    activation: dict[str, Any],
    assistant_data: dict[str, Any],
    run_key: str,
    job_name: str,
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

    entrypoint = activation.get("entrypoint") or request.entrypoint
    team_ids = assistant_data.get("team_ids") or []
    team_summaries = assistant_data.get("team_summaries") or []
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
            "OWNER_TEAM_ID": (
                str(assistant_data.get("owner_team_id"))
                if assistant_data.get("owner_team_id") is not None
                else ""
            ),
        },
    )
    destination = request.destination or activation.get("destination")
    if destination is not None:
        env["TASK_DESTINATION"] = str(destination)
    return env


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


def _diagnostic_needs_offline_job_status(diagnostic: dict[str, Any]) -> bool:
    """Return whether diagnosis should verify the stored Kubernetes job reference."""

    latest_run = diagnostic.get("latest_run")
    if not isinstance(latest_run, dict):
        return False
    if str(latest_run.get("execution_mode") or "") != "offline":
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
    diagnostic["health"] = _activation_health(
        activation=diagnostic.get("activation"),
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
    latest_run_job: dict[str, Any] | None = None,
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
        source_type=RunSource.scheduled,
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
        "job_lifecycle_safeguards": _offline_job_lifecycle_safeguards(),
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
        diagnostic = await asyncio.to_thread(
            _activation_materialization_diagnostic,
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
            detail=f"Task activation diagnosis failed while talking to Orchestra: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to diagnose task activation: {exc}",
        ) from exc


@router.post("/task-activation/reconcile-current")
async def reconcile_current_task_activation(request: TaskActivationDiagnosticRequest):
    """Diagnose one current activation and run the deterministic repair if safe."""

    diagnostic = await diagnose_task_activation(request)
    health = diagnostic.get("health") or {}
    if not health.get("repairable"):
        return {
            "success": True,
            "status": "noop",
            "reason": str(health.get("status") or "not_repairable"),
            "diagnostic": diagnostic,
            "summary": _task_activation_health_summary([diagnostic]),
        }
    repair = await repair_current_task_activation(request)
    return {
        "success": True,
        "status": "reconciled",
        "diagnostic": diagnostic,
        "repair": repair,
        "summary": _task_activation_health_summary([diagnostic]),
    }


@router.post("/task-activation/health")
async def task_activation_health(request: TaskActivationDiagnosticRequest):
    """Return alertable health counts for one source-aware activation diagnostic."""

    diagnostic = await diagnose_task_activation(request)
    return {
        "success": True,
        "diagnostics": [diagnostic],
        "summary": _task_activation_health_summary([diagnostic]),
    }


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
    if health_status in {"fired_failed_retryable", "stale_running_run"}:
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
    if request.source_type is RunSource.scheduled and request.scheduled_for is None:
        raise HTTPException(
            status_code=400,
            detail="Scheduled offline dispatch requires scheduled_for",
        )


@assistant_self_router.post("/task-activation/offline-dispatch")
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
            _resolve_offline_dispatch_activation,
            request,
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
        batch_api, core_api, _, _ = await _get_k8s_clients()
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
                    "job_status": job_status,
                }
            # The recorded Job is gone or terminal while the run row still
            # claims to be in flight: fail the stale row and launch a retry.
            retry_count = int(run.get("retry_count") or 0) + 1
            previous_error = str(run.get("error") or "")
            error = _stale_inflight_run_error(
                run_key=run_key,
                run_state=run_state,
                job_status=job_status,
            )
            await asyncio.to_thread(
                _update_task_run,
                assistant_id=request.assistant_id,
                run_key=run_key,
                updates=_failed_task_run_updates(
                    error=error,
                    result_summary=error,
                    retry_count=retry_count,
                    previous_error=previous_error,
                ),
            )
            _emit_task_activation_event(
                "task_activation.offline_dispatch.retrying",
                **_offline_dispatch_event_fields(
                    request,
                    stage=stage,
                    run_key=run_key,
                    job_name=job_name,
                    run_state=run_state,
                    status="retrying_stale_inflight_run",
                ),
            )

        stage = "launch_job"
        job_name = _build_offline_task_job_name(run_key, retry_count=retry_count)
        offline_env = _build_offline_runner_env(
            request=request,
            activation=activation or {},
            assistant_data=assistant_data,
            run_key=run_key,
            job_name=job_name,
        )
        _emit_task_activation_event(
            "task_activation.offline_dispatch.stage",
            **_offline_dispatch_event_fields(
                request,
                stage=stage,
                run_key=run_key,
                job_name=job_name,
            ),
        )
        max_runtime_raw = (activation or {}).get(
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
            _emit_task_activation_event(
                "task_activation.offline_dispatch.adopted",
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
                previous_error=previous_error,
            ),
        )
        _emit_task_activation_event(
            "task_activation.offline_dispatch.launched",
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
