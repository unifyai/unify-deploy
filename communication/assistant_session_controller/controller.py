from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import uuid

import kopf  # type: ignore[import-not-found]
from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.rest import ApiException

from common.settings import SETTINGS
from communication.infra.assistant_sessions import (
    assistant_session_observability_fields,
    assistant_session_desired_state,
    BINDING_ID_ANNOTATION,
    BINDING_ID_LABEL,
    binding_desktop_url,
    binding_id as binding_id_from_status,
    binding_job_ref,
    binding_pod_ref,
    binding_vm_ref,
    build_binding,
    CONTAINER_READY_ANNOTATION,
    DESIRED_STATE_STOPPED,
    SESSION_REF_ANNOTATION,
    SESSION_REF_LABEL,
    session_binding,
    session_desktop_mode,
    session_desktop_required,
    build_condition,
    emit_observability_event,
    get_assistant_session,
    merge_conditions,
    patch_assistant_session_status,
    read_bootstrap_secret,
)
from communication.infra.vm_helpers import (
    AssistantDiskInUseError,
    assign_pool_vm,
    complete_pool_vm_release,
    find_vm_with_disk,
    probe_vm_agent_service,
    release_pool_vm,
    replenish_pool,
    split_binding_runtime_vms,
    verify_vm_assignment,
)

DESKTOP_LIVENESS_FAILURE_THRESHOLD = int(
    os.environ.get("DESKTOP_LIVENESS_FAILURE_THRESHOLD", "3"),
)

logger = logging.getLogger(__name__)

WATCH_NAMESPACE = os.environ.get("WATCH_NAMESPACE", SETTINGS.default_namespace)
RECONCILE_INTERVAL_SECONDS = float(os.environ.get("SESSION_RECONCILE_INTERVAL", "5"))
CONTAINER_BOOTSTRAP_DEADLINE_SECONDS = float(
    os.environ.get("CONTAINER_BOOTSTRAP_DEADLINE_SECONDS", "90"),
)
MAX_BOOTSTRAP_RETRIES = int(os.environ.get("MAX_BOOTSTRAP_RETRIES", "2"))
VM_READINESS_DEADLINE_SECONDS = float(
    os.environ.get("VM_READINESS_DEADLINE_SECONDS", "60"),
)
MAX_VM_READINESS_RETRIES = int(os.environ.get("MAX_VM_READINESS_RETRIES", "2"))

_batch_api: k8s_client.BatchV1Api | None = None
_core_api: k8s_client.CoreV1Api | None = None
_custom_api: k8s_client.CustomObjectsApi | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_clients() -> None:
    global _batch_api, _core_api, _custom_api
    if _batch_api and _core_api and _custom_api:
        return
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    api_client = k8s_client.ApiClient()
    _batch_api = k8s_client.BatchV1Api(api_client)
    _core_api = k8s_client.CoreV1Api(api_client)
    _custom_api = k8s_client.CustomObjectsApi(api_client)


def _sanitize_for_k8s(value: str) -> str:
    return str(value).lower().replace("_", "-")


def _job_terminal_phase(job) -> str | None:
    labels = job.metadata.labels or {}
    if labels.get("unity-status") == "done":
        return "Succeeded"
    for condition in job.status.conditions or []:
        if condition.type == "Failed" and condition.status == "True":
            return "Failed"
        if condition.type == "Complete" and condition.status == "True":
            return "Succeeded"
    return None


def _current_pod_ref(job_name: str) -> dict | None:
    assert _core_api is not None
    pods = _core_api.list_namespaced_pod(
        namespace=WATCH_NAMESPACE,
        label_selector=f"job-name={job_name}",
    )
    for pod in pods.items:
        if pod.status.phase == "Running":
            return {"name": pod.metadata.name, "namespace": WATCH_NAMESPACE}
    if pods.items:
        return {"name": pods.items[0].metadata.name, "namespace": WATCH_NAMESPACE}
    return None


def _refresh_session_snapshot(body: dict) -> dict | None:
    assert _custom_api is not None
    assistant_id = str((body.get("spec") or {}).get("assistantId", ""))
    if not assistant_id:
        return body
    return get_assistant_session(_custom_api, WATCH_NAMESPACE, assistant_id)


_BINDING_UNSET = object()


def _binding_payload(
    binding: dict | None,
    *,
    binding_id: str | object = _BINDING_UNSET,
    job_ref: dict | None | object = _BINDING_UNSET,
    pod_ref: dict | None | object = _BINDING_UNSET,
    vm_ref: dict | None | object = _BINDING_UNSET,
    desktop_url: str | None | object = _BINDING_UNSET,
    created_at: str | None | object = _BINDING_UNSET,
    container_ready_at: str | None | object = _BINDING_UNSET,
    vm_assigned_at: str | None | object = _BINDING_UNSET,
    vm_ready_observed_at: str | None | object = _BINDING_UNSET,
    vm_ready_hostname: str | None | object = _BINDING_UNSET,
    vm_ready_message_id: str | None | object = _BINDING_UNSET,
    release_requested_at: str | None | object = _BINDING_UNSET,
    release_completed_at: str | None | object = _BINDING_UNSET,
) -> dict:
    """Return a canonical binding payload with selected fields overridden."""

    binding = binding or {}
    resolved_binding_id = (
        binding_id_from_status(binding)
        if binding_id is _BINDING_UNSET
        else str(binding_id)
    )
    return build_binding(
        binding_id=resolved_binding_id,
        job_ref=(
            binding_job_ref(binding) or None if job_ref is _BINDING_UNSET else job_ref
        ),
        pod_ref=(
            binding_pod_ref(binding) or None if pod_ref is _BINDING_UNSET else pod_ref
        ),
        vm_ref=binding_vm_ref(binding) or None if vm_ref is _BINDING_UNSET else vm_ref,
        desktop_url=(
            binding_desktop_url(binding) or None
            if desktop_url is _BINDING_UNSET
            else desktop_url
        ),
        created_at=(
            binding.get("createdAt") if created_at is _BINDING_UNSET else created_at
        ),
        container_ready_at=(
            binding.get("containerReadyAt")
            if container_ready_at is _BINDING_UNSET
            else container_ready_at
        ),
        vm_assigned_at=(
            binding.get("vmAssignedAt")
            if vm_assigned_at is _BINDING_UNSET
            else vm_assigned_at
        ),
        vm_ready_observed_at=(
            binding.get("vmReadyObservedAt")
            if vm_ready_observed_at is _BINDING_UNSET
            else vm_ready_observed_at
        ),
        vm_ready_hostname=(
            binding.get("vmReadyHostname")
            if vm_ready_hostname is _BINDING_UNSET
            else vm_ready_hostname
        ),
        vm_ready_message_id=(
            binding.get("vmReadyMessageId")
            if vm_ready_message_id is _BINDING_UNSET
            else vm_ready_message_id
        ),
        release_requested_at=(
            binding.get("releaseRequestedAt")
            if release_requested_at is _BINDING_UNSET
            else release_requested_at
        ),
        release_completed_at=(
            binding.get("releaseCompletedAt")
            if release_completed_at is _BINDING_UNSET
            else release_completed_at
        ),
    )


def _mint_binding_payload() -> dict:
    """Create a fresh controller-owned runtime binding."""

    return build_binding(
        binding_id=uuid.uuid4().hex,
        created_at=_now_iso(),
    )


def _parse_iso_or_none(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into UTC."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _binding_deadline_exceeded(
    binding: dict | None,
    timestamp_key: str,
    timeout_seconds: float,
) -> bool:
    """Return whether the binding has exceeded a phase deadline."""

    timestamp = _parse_iso_or_none(str((binding or {}).get(timestamp_key, "") or ""))
    if timestamp is None:
        return False
    elapsed = (datetime.now(timezone.utc) - timestamp).total_seconds()
    return elapsed > timeout_seconds


def _job_matches_binding(job, session_name: str, binding_id: str) -> bool:
    """Return whether a Job belongs to the current session binding."""

    labels = job.metadata.labels or {}
    annotations = job.metadata.annotations or {}
    return (
        labels.get(SESSION_REF_LABEL) == session_name
        and annotations.get(SESSION_REF_ANNOTATION) == session_name
        and labels.get(BINDING_ID_LABEL) == binding_id
        and annotations.get(BINDING_ID_ANNOTATION) == binding_id
    )


def _job_for_binding(session_name: str, binding: dict | None):
    """Load the current binding-owned Job."""

    assert _batch_api is not None
    job_name = str(binding_job_ref(binding).get("name", "") or "")
    current_binding_id = binding_id_from_status(binding)
    if not current_binding_id:
        return None
    if job_name:
        try:
            job = _batch_api.read_namespaced_job(
                name=job_name,
                namespace=WATCH_NAMESPACE,
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
            return None
        return (
            job if _job_matches_binding(job, session_name, current_binding_id) else None
        )

    jobs = _batch_api.list_namespaced_job(
        namespace=WATCH_NAMESPACE,
        label_selector=f"{SESSION_REF_LABEL}={session_name}",
    )
    for job in jobs.items:
        if _job_matches_binding(job, session_name, current_binding_id):
            return job
    return None


def _claim_idle_job_for_binding(assistant_id: str, session_name: str, binding: dict):
    """Claim exactly one idle Job for the current binding."""

    assert _batch_api is not None
    current_binding_id = binding_id_from_status(binding)
    if not current_binding_id:
        return None

    existing_job = _job_for_binding(session_name, binding)
    if existing_job is not None:
        return existing_job

    sanitized_assistant_id = _sanitize_for_k8s(assistant_id)
    jobs = _batch_api.list_namespaced_job(
        namespace=WATCH_NAMESPACE,
        label_selector="app=unity,unity-status=idle",
    )
    idle_jobs = sorted(
        (
            job
            for job in jobs.items
            if job.status.active
            and job.status.active > 0
            and not job.metadata.deletion_timestamp
        ),
        key=lambda job: str(job.metadata.name or ""),
    )

    for job in idle_jobs:
        labels = dict(job.metadata.labels or {})
        labels["assistant-id"] = sanitized_assistant_id
        labels["unity-status"] = "running"
        labels[SESSION_REF_LABEL] = session_name
        labels[BINDING_ID_LABEL] = current_binding_id
        annotations = dict(job.metadata.annotations or {})
        annotations[SESSION_REF_ANNOTATION] = session_name
        annotations[BINDING_ID_ANNOTATION] = current_binding_id
        annotations[CONTAINER_READY_ANNOTATION] = "false"
        body = {
            "metadata": {
                "labels": labels,
                "annotations": annotations,
                "resourceVersion": job.metadata.resource_version,
            },
        }
        try:
            _batch_api.patch_namespaced_job(
                name=job.metadata.name,
                namespace=WATCH_NAMESPACE,
                body=body,
            )
        except ApiException as exc:
            if exc.status == 409:
                existing_job = _job_for_binding(session_name, binding)
                if existing_job is not None:
                    return existing_job
                continue
            raise

        emit_observability_event(
            "controller.binding_job_claimed",
            assistant_id=assistant_id,
            session_name=session_name,
            binding_id=current_binding_id,
            job_name=job.metadata.name,
            source="controller.reconcile",
        )
        return _batch_api.read_namespaced_job(
            name=job.metadata.name,
            namespace=WATCH_NAMESPACE,
        )

    return None


def _suspend_bound_job(job, *, source: str) -> None:
    """Stop a binding-owned Job without mutating binding ownership."""

    assert _batch_api is not None
    labels = dict(job.metadata.labels or {})
    labels["unity-status"] = "done"
    body = {
        "metadata": {
            "labels": labels,
            "annotations": dict(job.metadata.annotations or {}),
        },
        "spec": {"suspend": True},
    }
    _batch_api.patch_namespaced_job(
        name=job.metadata.name,
        namespace=WATCH_NAMESPACE,
        body=body,
    )
    emit_observability_event(
        "controller.binding_job_suspended",
        job_name=job.metadata.name,
        source=source,
    )


def _condition_state(
    existing_conditions: list[dict],
    phase: str,
    desktop_required: bool,
    *,
    container_assigned: bool,
    container_ready: bool,
    vm_assigned: bool,
    desktop_ready: bool,
    reason: str,
    message: str,
) -> list[dict]:
    """Build the controller-owned condition set for the current phase."""

    updates = [
        build_condition(
            "ContainerAssigned",
            container_assigned,
            reason if not container_assigned else "Bound",
            message if not container_assigned else "Session job bound",
        ),
        build_condition(
            "ContainerReady",
            container_ready,
            reason,
            message,
        ),
        build_condition(
            "Active",
            phase == "Active",
            reason if phase != "Active" else "Ready",
            message,
        ),
    ]
    if desktop_required:
        updates.extend(
            [
                build_condition("VMAssigned", vm_assigned, reason, message),
                build_condition("DesktopReady", desktop_ready, reason, message),
            ],
        )
    return merge_conditions(existing_conditions, *updates)


def _owned_runtime_cleanup_state(
    assistant_id: str,
    binding_id: str | None = None,
) -> tuple[list[dict], list[dict], str | None]:
    """Return current-binding VMs, other assistant VMs, and attached disk holder."""

    current_binding_vms, other_binding_vms = split_binding_runtime_vms(
        assistant_id,
        binding_id=binding_id,
    )
    return current_binding_vms, other_binding_vms, find_vm_with_disk(assistant_id)


def _release_observability_fields(
    *,
    assistant_id: str,
    session_name: str,
    binding: dict,
    source_reason: str,
    job_live: bool,
    release_requested_at: str,
    release_completed_at: str,
    owned_runtime_vms: list[dict],
    other_runtime_vms: list[dict],
    disk_vm_name: str | None,
) -> dict:
    """Return consistent release-state observability fields."""

    return {
        "assistant_id": assistant_id,
        "session_name": session_name,
        "binding_id": binding_id_from_status(binding),
        "job_name": binding_job_ref(binding).get("name"),
        "pod_name": binding_pod_ref(binding).get("name"),
        "vm_name": binding_vm_ref(binding).get("name"),
        "vm_hostname": binding_vm_ref(binding).get("hostname"),
        "source_reason": source_reason,
        "job_live": job_live,
        "release_requested_at": release_requested_at or None,
        "release_completed_at": release_completed_at or None,
        "owned_runtime_vm_names": [
            str(vm.get("vm_name", "") or "") for vm in owned_runtime_vms
        ],
        "owned_runtime_vm_roles": {
            str(vm.get("vm_name", "") or ""): str(vm.get("pool_role", "") or "")
            for vm in owned_runtime_vms
            if vm.get("vm_name")
        },
        "other_runtime_vm_names": [
            str(vm.get("vm_name", "") or "") for vm in other_runtime_vms
        ],
        "other_runtime_vm_roles": {
            str(vm.get("vm_name", "") or ""): str(vm.get("pool_role", "") or "")
            for vm in other_runtime_vms
            if vm.get("vm_name")
        },
        "disk_vm_name": disk_vm_name or None,
    }


def _binding_release_state(
    *,
    assistant_id: str,
    session_name: str,
    binding: dict,
    existing_conditions: list[dict],
    desktop_required: bool,
    source_reason: str,
) -> tuple[str, dict, list[dict], str]:
    """Drive release until the binding is fully cleaned up."""

    current_binding_id = binding_id_from_status(binding)
    job = _job_for_binding(session_name, binding)
    job_live = job is not None and _job_terminal_phase(job) is None
    vm_ref = binding_vm_ref(binding)
    vm_name = str(vm_ref.get("name", "") or "")
    release_requested_at = str(binding.get("releaseRequestedAt", "") or "")
    release_completed_at = str(binding.get("releaseCompletedAt", "") or "")
    last_error = ""

    if job_live:
        try:
            _suspend_bound_job(job, source=f"controller.release.{source_reason}")
        except Exception as exc:  # pragma: no cover - best effort suspend
            logger.exception(
                "Failed to suspend Job %s during release",
                job.metadata.name,
            )
            last_error = str(exc)

    owned_runtime_vms, other_runtime_vms, disk_vm_name = _owned_runtime_cleanup_state(
        assistant_id,
        current_binding_id,
    )
    emit_observability_event(
        "controller.release_state.enter",
        **_release_observability_fields(
            assistant_id=assistant_id,
            session_name=session_name,
            binding=binding,
            source_reason=source_reason,
            job_live=job_live,
            release_requested_at=release_requested_at,
            release_completed_at=release_completed_at,
            owned_runtime_vms=owned_runtime_vms,
            other_runtime_vms=other_runtime_vms,
            disk_vm_name=disk_vm_name,
        ),
    )
    if (
        not job_live
        and not owned_runtime_vms
        and not other_runtime_vms
        and disk_vm_name is None
        and (vm_name or release_requested_at or release_completed_at)
    ):
        release_requested_at = release_requested_at or _now_iso()
        release_completed_at = release_completed_at or _now_iso()
        binding = _binding_payload(
            binding,
            vm_ref=None,
            desktop_url=None,
            release_requested_at=release_requested_at,
            release_completed_at=release_completed_at,
        )
        vm_name = ""

    if vm_name:
        if release_completed_at:
            emit_observability_event(
                "controller.release_state.awaiting_release_completion",
                **_release_observability_fields(
                    assistant_id=assistant_id,
                    session_name=session_name,
                    binding=binding,
                    source_reason=source_reason,
                    job_live=job_live,
                    release_requested_at=release_requested_at,
                    release_completed_at=release_completed_at,
                    owned_runtime_vms=owned_runtime_vms,
                    other_runtime_vms=other_runtime_vms,
                    disk_vm_name=disk_vm_name,
                ),
            )
            result = complete_pool_vm_release(vm_name, current_binding_id)
            emit_observability_event(
                "controller.release_state.complete_release_result",
                **_release_observability_fields(
                    assistant_id=assistant_id,
                    session_name=session_name,
                    binding=binding,
                    source_reason=source_reason,
                    job_live=job_live,
                    release_requested_at=release_requested_at,
                    release_completed_at=release_completed_at,
                    owned_runtime_vms=owned_runtime_vms,
                    other_runtime_vms=other_runtime_vms,
                    disk_vm_name=disk_vm_name,
                ),
                release_result=result,
            )
            if not result.get("skipped"):
                binding = _binding_payload(
                    binding,
                    vm_ref=None,
                    desktop_url=None,
                    release_completed_at=release_completed_at,
                )
            elif result.get("reason") == "binding_changed":
                binding = _binding_payload(
                    binding,
                    vm_ref=None,
                    desktop_url=None,
                )
        else:
            emit_observability_event(
                "controller.release_state.request_vm_release",
                **_release_observability_fields(
                    assistant_id=assistant_id,
                    session_name=session_name,
                    binding=binding,
                    source_reason=source_reason,
                    job_live=job_live,
                    release_requested_at=release_requested_at,
                    release_completed_at=release_completed_at,
                    owned_runtime_vms=owned_runtime_vms,
                    other_runtime_vms=other_runtime_vms,
                    disk_vm_name=disk_vm_name,
                ),
                release_retry=bool(release_requested_at),
            )
            result = release_pool_vm(
                assistant_id,
                current_binding_id,
                vm_name=vm_name,
            )
            emit_observability_event(
                "controller.release_state.request_vm_release_result",
                **_release_observability_fields(
                    assistant_id=assistant_id,
                    session_name=session_name,
                    binding=binding,
                    source_reason=source_reason,
                    job_live=job_live,
                    release_requested_at=release_requested_at,
                    release_completed_at=release_completed_at,
                    owned_runtime_vms=owned_runtime_vms,
                    other_runtime_vms=other_runtime_vms,
                    disk_vm_name=disk_vm_name,
                ),
                release_result=result,
                release_retry=bool(release_requested_at),
            )
            if result.get("released") or result.get("pool_role") == "releasing":
                if not release_requested_at:
                    release_requested_at = _now_iso()
                    binding = _binding_payload(
                        binding,
                        release_requested_at=release_requested_at,
                    )
            if result.get("retired"):
                release_completed_at = release_requested_at or _now_iso()
                binding = _binding_payload(
                    binding,
                    vm_ref=None,
                    desktop_url=None,
                    release_requested_at=release_requested_at,
                    release_completed_at=release_completed_at,
                )
    else:
        emit_observability_event(
            "controller.release_state.no_vm_ref",
            **_release_observability_fields(
                assistant_id=assistant_id,
                session_name=session_name,
                binding=binding,
                source_reason=source_reason,
                job_live=job_live,
                release_requested_at=release_requested_at,
                release_completed_at=release_completed_at,
                owned_runtime_vms=owned_runtime_vms,
                other_runtime_vms=other_runtime_vms,
                disk_vm_name=disk_vm_name,
            ),
        )
        if not release_requested_at:
            release_requested_at = _now_iso()
        if not release_completed_at and not owned_runtime_vms and disk_vm_name is None:
            release_completed_at = _now_iso()
        binding = _binding_payload(
            binding,
            release_requested_at=release_requested_at,
            release_completed_at=release_completed_at,
        )

    refreshed_job = _job_for_binding(session_name, binding)
    refreshed_job_live = (
        refreshed_job is not None and _job_terminal_phase(refreshed_job) is None
    )
    cleaned_vm_ref = binding_vm_ref(binding)
    remaining_runtime_vms, remaining_other_runtime_vms, remaining_disk_vm_name = (
        _owned_runtime_cleanup_state(
            assistant_id,
            current_binding_id,
        )
    )
    release_complete = (
        bool(binding.get("releaseCompletedAt"))
        and not refreshed_job_live
        and not cleaned_vm_ref
        and not remaining_runtime_vms
        and not remaining_other_runtime_vms
        and remaining_disk_vm_name is None
    )
    emit_observability_event(
        "controller.release_state.result",
        **_release_observability_fields(
            assistant_id=assistant_id,
            session_name=session_name,
            binding=binding,
            source_reason=source_reason,
            job_live=refreshed_job_live,
            release_requested_at=str(binding.get("releaseRequestedAt", "") or ""),
            release_completed_at=str(binding.get("releaseCompletedAt", "") or ""),
            owned_runtime_vms=remaining_runtime_vms,
            other_runtime_vms=remaining_other_runtime_vms,
            disk_vm_name=remaining_disk_vm_name,
        ),
        release_complete=release_complete,
        cleaned_vm_name=cleaned_vm_ref.get("name"),
    )
    if release_complete:
        released_conditions = _condition_state(
            existing_conditions,
            "Released",
            desktop_required,
            container_assigned=False,
            container_ready=False,
            vm_assigned=False,
            desktop_ready=False,
            reason="Released",
            message="Runtime cleanup complete",
        )
        return "Released", None, released_conditions, last_error

    releasing_conditions = _condition_state(
        existing_conditions,
        "Releasing",
        desktop_required,
        container_assigned=bool(refreshed_job_live),
        container_ready=False,
        vm_assigned=bool(cleaned_vm_ref),
        desktop_ready=False,
        reason="ReleaseRequested",
        message="Runtime cleanup in progress",
    )
    return "Releasing", binding, releasing_conditions, last_error


def _restart_binding_decision(
    *,
    assistant_id: str,
    activation_id: str,
    retry_count: int,
    max_retries: int,
    retry_field: str,
    message: str,
) -> dict:
    """Return the next binding transition after a recoverable runtime failure."""

    if retry_count >= max_retries:
        return {
            "phase": "Failed",
            "binding": None,
            "last_error": message,
            retry_field: retry_count,
        }
    return {
        "phase": "PendingJob",
        "binding": _mint_binding_payload(),
        "last_error": message,
        "observed_activation_id": activation_id,
        retry_field: retry_count + 1,
    }


def _update_status_for_session(body: dict) -> None:  # type: ignore[override]
    """Binding-authoritative AssistantSession reconcile loop."""

    assert _custom_api is not None
    assert _core_api is not None

    latest_body = _refresh_session_snapshot(body)
    if latest_body is None:
        return
    body = latest_body

    session_name = str(body.get("metadata", {}).get("name", "") or "")
    spec = body.get("spec", {})
    status = body.get("status", {})
    assistant_id = str(spec.get("assistantId", "") or "")
    activation_id = str(spec.get("activationId", "") or "")
    desired_state = assistant_session_desired_state(body)
    desktop_required = session_desktop_required(body)
    desktop_mode = session_desktop_mode(body)
    secret_name = str(spec.get("startupSecretRef", "") or "")
    observed_activation_id = str(status.get("observedActivationId", "") or "")
    existing_conditions = status.get("conditions", [])
    binding = session_binding(body)
    bootstrap_retries = int(status.get("bootstrapRetries", 0) or 0)
    vm_retries = int(status.get("vmRetries", 0) or 0)
    desktop_probe_failures = int(status.get("desktopProbeFailures", 0) or 0)

    emit_observability_event(
        "controller.session_reconcile",
        **assistant_session_observability_fields(
            body,
            source="controller.reconcile",
        ),
    )

    if not assistant_id or not activation_id or not secret_name:
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="Failed",
            binding=None,
            last_error="AssistantSession missing required spec fields",
            source="controller.reconcile",
            conditions=_condition_state(
                existing_conditions,
                "Failed",
                desktop_required,
                container_assigned=False,
                container_ready=False,
                vm_assigned=False,
                desktop_ready=False,
                reason="InvalidSpec",
                message="Missing required AssistantSession spec fields",
            ),
        )
        return

    current_binding_id = binding_id_from_status(binding)
    phase = str(status.get("phase", "") or "")

    if (
        desired_state != DESIRED_STATE_STOPPED
        and phase == "Failed"
        and not current_binding_id
        and observed_activation_id == activation_id
    ):
        return

    if desired_state == DESIRED_STATE_STOPPED:
        if not current_binding_id:
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="Released",
                observed_activation_id=observed_activation_id or activation_id,
                binding=None,
                last_error="",
                source="controller.reconcile",
                conditions=_condition_state(
                    existing_conditions,
                    "Released",
                    desktop_required,
                    container_assigned=False,
                    container_ready=False,
                    vm_assigned=False,
                    desktop_ready=False,
                    reason="Released",
                    message="Session is stopped",
                ),
            )
            return
        release_phase, release_binding, release_conditions, release_error = (
            _binding_release_state(
                assistant_id=assistant_id,
                session_name=session_name,
                binding=binding,
                existing_conditions=existing_conditions,
                desktop_required=desktop_required,
                source_reason="desired_stop",
            )
        )
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase=release_phase,
            observed_activation_id=observed_activation_id or activation_id,
            binding=release_binding if release_binding else None,
            last_error=release_error if release_error else "",
            source="controller.reconcile",
            conditions=release_conditions,
        )
        return

    if (
        phase == "Failed"
        and not current_binding_id
        and observed_activation_id == activation_id
    ):
        return

    if (
        current_binding_id
        and observed_activation_id
        and observed_activation_id != activation_id
    ):
        release_phase, release_binding, release_conditions, release_error = (
            _binding_release_state(
                assistant_id=assistant_id,
                session_name=session_name,
                binding=binding,
                existing_conditions=existing_conditions,
                desktop_required=desktop_required,
                source_reason="activation_replacement",
            )
        )
        if release_phase != "Released":
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase=release_phase,
                observed_activation_id=observed_activation_id,
                binding=release_binding if release_binding else None,
                last_error=release_error
                or "Releasing prior binding for activation replacement",
                source="controller.reconcile",
                conditions=release_conditions,
            )
            return
        binding = {}
        current_binding_id = ""
        existing_conditions = release_conditions
        phase = "Released"

    if phase == "Releasing" and current_binding_id:
        release_phase, release_binding, release_conditions, release_error = (
            _binding_release_state(
                assistant_id=assistant_id,
                session_name=session_name,
                binding=binding,
                existing_conditions=existing_conditions,
                desktop_required=desktop_required,
                source_reason="continue_release",
            )
        )
        if release_phase != "Released":
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase=release_phase,
                observed_activation_id=observed_activation_id or activation_id,
                binding=release_binding if release_binding else None,
                last_error=release_error or "",
                source="controller.reconcile",
                conditions=release_conditions,
            )
            return
        binding = {}
        current_binding_id = ""
        existing_conditions = release_conditions
        phase = "Released"

    if not current_binding_id:
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="PendingJob",
            observed_activation_id=activation_id,
            binding=_mint_binding_payload(),
            last_error="",
            source="controller.reconcile",
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
            desktop_probe_failures=0,
            conditions=_condition_state(
                [],
                "PendingJob",
                desktop_required,
                container_assigned=False,
                container_ready=False,
                vm_assigned=False,
                desktop_ready=False,
                reason="BindingCreated",
                message="Waiting to claim an idle Unity container",
            ),
        )
        return

    job = _job_for_binding(session_name, binding)
    if binding_job_ref(binding) and job is None:
        release_phase, release_binding, release_conditions, release_error = (
            _binding_release_state(
                assistant_id=assistant_id,
                session_name=session_name,
                binding=binding,
                existing_conditions=existing_conditions,
                desktop_required=desktop_required,
                source_reason="job_missing",
            )
        )
        if release_phase != "Released":
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase=release_phase,
                observed_activation_id=activation_id,
                binding=release_binding if release_binding else None,
                last_error=release_error
                or "Recorded binding Job disappeared before runtime cleanup completed",
                source="controller.reconcile",
                bootstrap_retries=bootstrap_retries,
                vm_retries=vm_retries,
                desktop_probe_failures=0,
                conditions=release_conditions,
            )
            return
        decision = _restart_binding_decision(
            assistant_id=assistant_id,
            activation_id=activation_id,
            retry_count=bootstrap_retries,
            max_retries=MAX_BOOTSTRAP_RETRIES,
            retry_field="bootstrap_retries",
            message="Recorded binding Job disappeared before runtime became ready",
        )
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase=decision["phase"],
            observed_activation_id=activation_id,
            binding=decision["binding"],
            last_error=decision["last_error"],
            source="controller.reconcile",
            bootstrap_retries=decision["bootstrap_retries"],
            vm_retries=vm_retries,
            desktop_probe_failures=0,
            conditions=_condition_state(
                release_conditions,
                decision["phase"],
                desktop_required,
                container_assigned=False,
                container_ready=False,
                vm_assigned=False,
                desktop_ready=False,
                reason="JobMissing",
                message=decision["last_error"],
            ),
        )
        return

    if not binding_job_ref(binding):
        claimed_job = _claim_idle_job_for_binding(assistant_id, session_name, binding)
        if claimed_job is None:
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="PendingJob",
                observed_activation_id=activation_id,
                binding=binding,
                last_error="",
                source="controller.reconcile",
                conditions=_condition_state(
                    existing_conditions,
                    "PendingJob",
                    desktop_required,
                    container_assigned=False,
                    container_ready=False,
                    vm_assigned=False,
                    desktop_ready=False,
                    reason="WaitingForCapacity",
                    message="Waiting for idle Unity container capacity",
                ),
            )
            return
        binding = _binding_payload(
            binding,
            job_ref={"name": claimed_job.metadata.name, "namespace": WATCH_NAMESPACE},
            pod_ref=_current_pod_ref(claimed_job.metadata.name),
        )
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="PendingContainer",
            observed_activation_id=activation_id,
            binding=binding,
            last_error="",
            source="controller.reconcile",
            conditions=_condition_state(
                existing_conditions,
                "PendingContainer",
                desktop_required,
                container_assigned=True,
                container_ready=False,
                vm_assigned=False,
                desktop_ready=False,
                reason="WaitingForUnity",
                message="Waiting for Unity container bootstrap",
            ),
        )
        return

    assert job is not None
    pod_ref = _current_pod_ref(job.metadata.name)
    binding = _binding_payload(
        binding,
        job_ref={"name": job.metadata.name, "namespace": WATCH_NAMESPACE},
        pod_ref=pod_ref,
    )
    terminal_phase = _job_terminal_phase(job)
    if terminal_phase:
        release_phase, release_binding, release_conditions, release_error = (
            _binding_release_state(
                assistant_id=assistant_id,
                session_name=session_name,
                binding=binding,
                existing_conditions=existing_conditions,
                desktop_required=desktop_required,
                source_reason=f"job_terminal_{terminal_phase.lower()}",
            )
        )
        if release_phase != "Released":
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase=release_phase,
                observed_activation_id=activation_id,
                binding=release_binding if release_binding else None,
                last_error=release_error
                or f"Job reached terminal phase {terminal_phase}",
                source="controller.reconcile",
                conditions=release_conditions,
            )
            return
        decision = _restart_binding_decision(
            assistant_id=assistant_id,
            activation_id=activation_id,
            retry_count=bootstrap_retries,
            max_retries=MAX_BOOTSTRAP_RETRIES,
            retry_field="bootstrap_retries",
            message=f"Job reached terminal phase {terminal_phase}",
        )
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase=decision["phase"],
            observed_activation_id=activation_id,
            binding=decision["binding"],
            last_error=decision["last_error"],
            source="controller.reconcile",
            bootstrap_retries=decision["bootstrap_retries"],
            vm_retries=vm_retries,
            desktop_probe_failures=0,
            conditions=_condition_state(
                release_conditions,
                decision["phase"],
                desktop_required,
                container_assigned=False,
                container_ready=False,
                vm_assigned=False,
                desktop_ready=False,
                reason="JobTerminal",
                message=decision["last_error"],
            ),
        )
        return

    container_ready = (job.metadata.annotations or {}).get(
        CONTAINER_READY_ANNOTATION,
    ) == "true"
    if not container_ready:
        if _binding_deadline_exceeded(
            binding,
            "createdAt",
            CONTAINER_BOOTSTRAP_DEADLINE_SECONDS,
        ):
            try:
                _suspend_bound_job(job, source="controller.bootstrap_timeout")
            except Exception:  # pragma: no cover - best effort suspend
                logger.exception(
                    "Failed to suspend bootstrap-timed-out job %s",
                    job.metadata.name,
                )
            decision = _restart_binding_decision(
                assistant_id=assistant_id,
                activation_id=activation_id,
                retry_count=bootstrap_retries,
                max_retries=MAX_BOOTSTRAP_RETRIES,
                retry_field="bootstrap_retries",
                message=(
                    f"Container did not become ready within "
                    f"{int(CONTAINER_BOOTSTRAP_DEADLINE_SECONDS)}s"
                ),
            )
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase=decision["phase"],
                observed_activation_id=activation_id,
                binding=decision["binding"],
                last_error=decision["last_error"],
                source="controller.reconcile",
                bootstrap_retries=decision["bootstrap_retries"],
                vm_retries=vm_retries,
                desktop_probe_failures=0,
                conditions=_condition_state(
                    existing_conditions,
                    decision["phase"],
                    desktop_required,
                    container_assigned=False,
                    container_ready=False,
                    vm_assigned=False,
                    desktop_ready=False,
                    reason="BootstrapTimeout",
                    message=decision["last_error"],
                ),
            )
            return
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="PendingContainer",
            observed_activation_id=activation_id,
            binding=binding,
            last_error="",
            source="controller.reconcile",
            conditions=_condition_state(
                existing_conditions,
                "PendingContainer",
                desktop_required,
                container_assigned=True,
                container_ready=False,
                vm_assigned=False,
                desktop_ready=False,
                reason="WaitingForUnity",
                message="Unity has not yet signaled container-ready",
            ),
        )
        return

    if not binding.get("containerReadyAt"):
        binding = _binding_payload(
            binding,
            container_ready_at=_now_iso(),
        )

    if not desktop_required:
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="Active",
            observed_activation_id=activation_id,
            binding=_binding_payload(binding, vm_ref=None, desktop_url=None),
            last_error="",
            source="controller.reconcile",
            desktop_probe_failures=0,
            conditions=_condition_state(
                existing_conditions,
                "Active",
                desktop_required,
                container_assigned=True,
                container_ready=True,
                vm_assigned=False,
                desktop_ready=False,
                reason="Ready",
                message="Container session active",
            ),
        )
        return

    current_vm_ref = binding_vm_ref(binding)
    if not current_vm_ref:
        startup_payload = read_bootstrap_secret(_core_api, WATCH_NAMESPACE, secret_name)
        api_key = str(startup_payload.get("api_key", "") or "")
        try:
            result = assign_pool_vm(
                assistant_id=assistant_id,
                binding_id=current_binding_id,
                unify_apikey=api_key,
                vm_type=desktop_mode or "ubuntu",
            )
        except ValueError as exc:
            replenish_pool(desktop_mode or "ubuntu")
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="PendingVM",
                observed_activation_id=activation_id,
                binding=binding,
                last_error=str(exc),
                source="controller.reconcile",
                conditions=_condition_state(
                    existing_conditions,
                    "PendingVM",
                    desktop_required,
                    container_assigned=True,
                    container_ready=True,
                    vm_assigned=False,
                    desktop_ready=False,
                    reason="WaitingForCapacity",
                    message=str(exc),
                ),
            )
            return
        except AssistantDiskInUseError as exc:
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="PendingVM",
                observed_activation_id=activation_id,
                binding=binding,
                last_error=str(exc),
                source="controller.reconcile",
                conditions=_condition_state(
                    existing_conditions,
                    "PendingVM",
                    desktop_required,
                    container_assigned=True,
                    container_ready=True,
                    vm_assigned=False,
                    desktop_ready=False,
                    reason="WaitingForRelease",
                    message=str(exc),
                ),
            )
            return
        except Exception as exc:  # pragma: no cover - defensive reconcile
            logger.exception("AssistantSession VM assignment failed")
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="PendingVM",
                observed_activation_id=activation_id,
                binding=binding,
                last_error=str(exc),
                source="controller.reconcile",
                conditions=_condition_state(
                    existing_conditions,
                    "PendingVM",
                    desktop_required,
                    container_assigned=True,
                    container_ready=True,
                    vm_assigned=False,
                    desktop_ready=False,
                    reason="AssignError",
                    message=str(exc),
                ),
            )
            return
        latest_after_assign = get_assistant_session(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
        )
        latest_after_assign_spec = (latest_after_assign or {}).get("spec") or {}
        latest_after_assign_status = (latest_after_assign or {}).get("status") or {}
        latest_after_assign_binding = session_binding(latest_after_assign)
        emit_observability_event(
            "controller.vm_assignment.completed",
            assistant_id=assistant_id,
            session_name=session_name,
            binding_id=current_binding_id,
            assigned_vm_name=result["vm_name"],
            assigned_vm_hostname=result["hostname"],
            latest_session_present=latest_after_assign is not None,
            latest_desired_state=(
                str(latest_after_assign_spec.get("desiredState", "") or "") or None
            ),
            latest_phase=str(latest_after_assign_status.get("phase", "") or "") or None,
            latest_observed_activation_id=(
                str(latest_after_assign_status.get("observedActivationId", "") or "")
                or None
            ),
            latest_binding_id=binding_id_from_status(latest_after_assign_binding)
            or None,
            latest_job_name=(
                binding_job_ref(latest_after_assign_binding).get("name") or None
            ),
            latest_vm_name=(
                binding_vm_ref(latest_after_assign_binding).get("name") or None
            ),
            desired_state_drifted=(
                str(latest_after_assign_spec.get("desiredState", "") or "")
                not in ("", assistant_session_desired_state(body))
            ),
            binding_superseded=(
                binding_id_from_status(latest_after_assign_binding)
                not in ("", current_binding_id)
            ),
        )
        binding = _binding_payload(
            binding,
            vm_ref={
                "name": result["vm_name"],
                "hostname": result["hostname"],
                "vmType": desktop_mode or "ubuntu",
            },
            vm_assigned_at=_now_iso(),
            desktop_url=None,
            vm_ready_observed_at=None,
            vm_ready_hostname=None,
            vm_ready_message_id=None,
            release_requested_at=None,
            release_completed_at=None,
        )
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="PendingGuest",
            observed_activation_id=activation_id,
            binding=binding,
            last_error="",
            source="controller.reconcile",
            conditions=_condition_state(
                existing_conditions,
                "PendingGuest",
                desktop_required,
                container_assigned=True,
                container_ready=True,
                vm_assigned=True,
                desktop_ready=False,
                reason="WaitingForDesktop",
                message="Waiting for authenticated desktop readiness",
            ),
        )
        return

    verified_vm_ref = verify_vm_assignment(
        str(current_vm_ref.get("name", "") or ""),
        current_binding_id,
        assistant_id,
    )
    if verified_vm_ref is None:
        try:
            _suspend_bound_job(job, source="controller.vm_ownership_lost")
        except Exception:  # pragma: no cover - best effort suspend
            logger.exception(
                "Failed to suspend Job %s after VM ownership loss",
                job.metadata.name,
            )
        decision = _restart_binding_decision(
            assistant_id=assistant_id,
            activation_id=activation_id,
            retry_count=vm_retries,
            max_retries=MAX_VM_READINESS_RETRIES,
            retry_field="vm_retries",
            message="Binding lost VM ownership before desktop became ready",
        )
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase=decision["phase"],
            observed_activation_id=activation_id,
            binding=decision["binding"],
            last_error=decision["last_error"],
            source="controller.reconcile",
            bootstrap_retries=bootstrap_retries,
            vm_retries=decision["vm_retries"],
            desktop_probe_failures=0,
            conditions=_condition_state(
                existing_conditions,
                decision["phase"],
                desktop_required,
                container_assigned=False,
                container_ready=False,
                vm_assigned=False,
                desktop_ready=False,
                reason="VMOwnershipLost",
                message=decision["last_error"],
            ),
        )
        return

    binding = _binding_payload(binding, vm_ref=verified_vm_ref)
    desktop_url = binding_desktop_url(binding)
    desktop_ready_signal = bool(binding.get("vmReadyObservedAt")) and bool(desktop_url)
    if desktop_ready_signal:
        vm_hostname = str(verified_vm_ref.get("hostname", "") or "")
        alive = probe_vm_agent_service(vm_hostname, timeout=3.0)
        if not alive:
            probe_failures = desktop_probe_failures + 1
            if probe_failures >= DESKTOP_LIVENESS_FAILURE_THRESHOLD:
                release_phase, release_binding, release_conditions, release_error = (
                    _binding_release_state(
                        assistant_id=assistant_id,
                        session_name=session_name,
                        binding=binding,
                        existing_conditions=existing_conditions,
                        desktop_required=desktop_required,
                        source_reason="desktop_liveness_failed",
                    )
                )
                if release_phase != "Released":
                    patch_assistant_session_status(
                        _custom_api,
                        WATCH_NAMESPACE,
                        assistant_id,
                        phase=release_phase,
                        observed_activation_id=activation_id,
                        binding=release_binding if release_binding else None,
                        last_error=release_error
                        or "Desktop liveness failed; replacing binding",
                        source="controller.reconcile",
                        bootstrap_retries=bootstrap_retries,
                        vm_retries=vm_retries + 1,
                        desktop_probe_failures=0,
                        conditions=release_conditions,
                    )
                    return
                decision = _restart_binding_decision(
                    assistant_id=assistant_id,
                    activation_id=activation_id,
                    retry_count=vm_retries,
                    max_retries=MAX_VM_READINESS_RETRIES,
                    retry_field="vm_retries",
                    message="Desktop VM became unreachable after readiness",
                )
                patch_assistant_session_status(
                    _custom_api,
                    WATCH_NAMESPACE,
                    assistant_id,
                    phase=decision["phase"],
                    observed_activation_id=activation_id,
                    binding=decision["binding"],
                    last_error=decision["last_error"],
                    source="controller.reconcile",
                    bootstrap_retries=bootstrap_retries,
                    vm_retries=decision["vm_retries"],
                    desktop_probe_failures=0,
                    conditions=_condition_state(
                        release_conditions,
                        decision["phase"],
                        desktop_required,
                        container_assigned=False,
                        container_ready=False,
                        vm_assigned=False,
                        desktop_ready=False,
                        reason="DesktopLost",
                        message=decision["last_error"],
                    ),
                )
                return
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="Active",
                observed_activation_id=activation_id,
                binding=binding,
                last_error="",
                source="controller.reconcile",
                bootstrap_retries=bootstrap_retries,
                vm_retries=vm_retries,
                desktop_probe_failures=probe_failures,
                conditions=_condition_state(
                    existing_conditions,
                    "Active",
                    desktop_required,
                    container_assigned=True,
                    container_ready=True,
                    vm_assigned=True,
                    desktop_ready=True,
                    reason="Ready",
                    message="Desktop session active",
                ),
            )
            return

        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="Active",
            observed_activation_id=activation_id,
            binding=binding,
            last_error="",
            source="controller.reconcile",
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
            desktop_probe_failures=0,
            conditions=_condition_state(
                existing_conditions,
                "Active",
                desktop_required,
                container_assigned=True,
                container_ready=True,
                vm_assigned=True,
                desktop_ready=True,
                reason="Ready",
                message="Desktop session active",
            ),
        )
        return

    if _binding_deadline_exceeded(
        binding,
        "vmAssignedAt",
        VM_READINESS_DEADLINE_SECONDS,
    ):
        release_phase, release_binding, release_conditions, release_error = (
            _binding_release_state(
                assistant_id=assistant_id,
                session_name=session_name,
                binding=binding,
                existing_conditions=existing_conditions,
                desktop_required=desktop_required,
                source_reason="vm_readiness_timeout",
            )
        )
        if release_phase != "Released":
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase=release_phase,
                observed_activation_id=activation_id,
                binding=release_binding if release_binding else None,
                last_error=release_error or "Waiting for desktop readiness timed out",
                source="controller.reconcile",
                bootstrap_retries=bootstrap_retries,
                vm_retries=vm_retries + 1,
                desktop_probe_failures=0,
                conditions=release_conditions,
            )
            return
        decision = _restart_binding_decision(
            assistant_id=assistant_id,
            activation_id=activation_id,
            retry_count=vm_retries,
            max_retries=MAX_VM_READINESS_RETRIES,
            retry_field="vm_retries",
            message=(
                f"VM did not become ready within "
                f"{int(VM_READINESS_DEADLINE_SECONDS)}s"
            ),
        )
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase=decision["phase"],
            observed_activation_id=activation_id,
            binding=decision["binding"],
            last_error=decision["last_error"],
            source="controller.reconcile",
            bootstrap_retries=bootstrap_retries,
            vm_retries=decision["vm_retries"],
            desktop_probe_failures=0,
            conditions=_condition_state(
                release_conditions,
                decision["phase"],
                desktop_required,
                container_assigned=False,
                container_ready=False,
                vm_assigned=False,
                desktop_ready=False,
                reason="ReadinessTimeout",
                message=decision["last_error"],
            ),
        )
        return

    patch_assistant_session_status(
        _custom_api,
        WATCH_NAMESPACE,
        assistant_id,
        phase="PendingGuest",
        observed_activation_id=activation_id,
        binding=binding,
        last_error="",
        source="controller.reconcile",
        bootstrap_retries=bootstrap_retries,
        vm_retries=vm_retries,
        desktop_probe_failures=0,
        conditions=_condition_state(
            existing_conditions,
            "PendingGuest",
            desktop_required,
            container_assigned=True,
            container_ready=True,
            vm_assigned=True,
            desktop_ready=False,
            reason="WaitingForDesktop",
            message="Waiting for authenticated desktop readiness",
        ),
    )


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    settings.posting.enabled = False
    logging.getLogger("kopf").setLevel(logging.WARNING)
    _load_clients()


@kopf.on.create(
    SETTINGS.assistant_session_group,
    SETTINGS.assistant_session_version,
    SETTINGS.assistant_session_plural,
)
@kopf.on.update(
    SETTINGS.assistant_session_group,
    SETTINGS.assistant_session_version,
    SETTINGS.assistant_session_plural,
)
def on_session_change(body, **_):
    _update_status_for_session(body)


@kopf.timer(
    SETTINGS.assistant_session_group,
    SETTINGS.assistant_session_version,
    SETTINGS.assistant_session_plural,
    interval=RECONCILE_INTERVAL_SECONDS,
)
def reconcile_session(body, **_):
    _update_status_for_session(body)


@kopf.on.delete(
    SETTINGS.assistant_session_group,
    SETTINGS.assistant_session_version,
    SETTINGS.assistant_session_plural,
)
def delete_session(body, **_):
    assert _batch_api is not None
    assert _core_api is not None
    session_name = str(body.get("metadata", {}).get("name", ""))
    spec = body.get("spec", {})
    assistant_id = str(spec.get("assistantId", ""))
    secret_name = spec.get("startupSecretRef")
    binding = session_binding(body)
    current_binding_id = binding_id_from_status(binding)
    try:
        job = _job_for_binding(session_name, binding)
    except Exception:  # pragma: no cover - best effort cleanup
        logger.exception("Failed to load bound job for deleted AssistantSession")
        job = None
    if job is not None:
        try:
            _suspend_bound_job(job, source="controller.session_delete")
        except Exception:  # pragma: no cover - best effort cleanup
            logger.exception("Failed to suspend job for deleted AssistantSession")
    if secret_name:
        try:
            _core_api.delete_namespaced_secret(
                name=secret_name,
                namespace=WATCH_NAMESPACE,
            )
        except ApiException as e:
            if e.status != 404:
                logger.exception(
                    "Failed deleting bootstrap secret for deleted AssistantSession",
                )
    if assistant_id and current_binding_id:
        current_vm_ref = binding_vm_ref(binding)
        try:
            if current_vm_ref.get("name"):
                release_pool_vm(
                    assistant_id,
                    current_binding_id,
                    vm_name=current_vm_ref["name"],
                )
        except Exception:  # pragma: no cover - best effort cleanup
            logger.exception("Failed releasing VM for deleted AssistantSession")


@kopf.on.probe(id="health")
def health_probe(**_):
    return {"ts": _now_iso(), "namespace": WATCH_NAMESPACE}
