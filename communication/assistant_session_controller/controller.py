from __future__ import annotations

import logging
import time
from typing import Literal
import uuid

from google.api_core.exceptions import GoogleAPICallError
import kopf  # type: ignore[import-not-found]
from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.client.rest import ApiException

from common.settings import SETTINGS
from communication.infra.helpers import (
    acquire_assignment_lease,
    release_assignment_lease,
)
from communication.infra.assistant_sessions import (
    assistant_session_observability_fields,
    BINDING_ID_ANNOTATION,
    BINDING_ID_LABEL,
    binding_desktop_url,
    binding_id as binding_id_from_status,
    binding_job_ref,
    binding_pod_ref,
    binding_vm_ref,
    CONTAINER_READY_ANNOTATION,
    DESIRED_STATE_STOPPED,
    SIGNAL_DESKTOP_READY,
    SIGNAL_VM_ASSIGNMENT,
    SIGNAL_VM_GUEST_HEALTH,
    SIGNAL_VM_RELEASE_COMPLETE,
    SIGNAL_VM_RELEASE_REQUEST,
    SESSION_REF_ANNOTATION,
    SESSION_REF_LABEL,
    session_binding,
    build_condition,
    emit_observability_event,
    get_assistant_session,
    merge_conditions,
    patch_assistant_session_status,
)
from communication.infra.vm_helpers import (
    complete_pool_vm_release,
    find_vm_with_disk,
    POOL_ROLE_RELEASING,
    release_pool_vm,
    split_binding_runtime_vms,
    verify_vm_assignment,
)
from communication.assistant_session_controller.workers import (
    schedule_guest_health_probe,
    schedule_vm_assignment,
    schedule_vm_release_request,
    worker_runtime_stats,
)
from communication.assistant_session_controller.binding_ops import (
    binding_deadline_exceeded as _binding_deadline_exceeded,
    binding_payload as _binding_payload,
    binding_signal_matches as _binding_signal_matches,
    mint_binding_payload as _mint_binding_payload,
    now_iso as _now_iso,
    signal_age_seconds as _signal_age_seconds,
    signal_by_name as _signal_by_name,
    signals_without as _signals_without,
)
from communication.assistant_session_controller.config import ControllerConfig
from communication.assistant_session_controller.session_snapshot import SessionSnapshot

CONFIG = ControllerConfig.from_env()

logger = logging.getLogger(__name__)

DESKTOP_LIVENESS_FAILURE_THRESHOLD = CONFIG.desktop_liveness_failure_threshold

_batch_api: k8s_client.BatchV1Api | None = None
_core_api: k8s_client.CoreV1Api | None = None
_custom_api: k8s_client.CustomObjectsApi | None = None
_coord_api: k8s_client.CoordinationV1Api | None = None

WATCH_NAMESPACE = CONFIG.watch_namespace
RECONCILE_INTERVAL_SECONDS = CONFIG.reconcile_interval_seconds
CONTAINER_BOOTSTRAP_DEADLINE_SECONDS = CONFIG.container_bootstrap_deadline_seconds
MAX_BOOTSTRAP_RETRIES = CONFIG.max_bootstrap_retries
VM_READINESS_DEADLINE_SECONDS = CONFIG.vm_readiness_deadline_seconds
MAX_VM_READINESS_RETRIES = CONFIG.max_vm_readiness_retries
VM_ASSIGNMENT_RETRY_INTERVAL_SECONDS = CONFIG.vm_assignment_retry_interval_seconds
_IMAGE_HASH_BUCKET = CONFIG.image_hash_bucket
_IMAGE_HASH_CACHE_TTL = CONFIG.image_hash_cache_ttl
_cached_image_hash: str | None = None
_image_hash_fetched_at: float = 0
_JOB_CLAIM_LEASE_DURATION_SECONDS = CONFIG.job_claim_lease_duration_seconds
_JOB_CLAIM_RESULT_CLAIMED = "claimed"
_JOB_CLAIM_RESULT_CAPACITY = "capacity"
_JOB_CLAIM_RESULT_BUSY = "busy"
JobClaimTransitionResult = Literal[
    "claimed",
    "capacity",
    "busy",
]


def _get_current_image_hash() -> str | None:
    """Return the latest Unity image commit hash from GCS, cached for TTL seconds.

    The controller only claims idle Jobs whose ``unity-image-hash`` label
    matches this value, preventing stale-image containers from being
    assigned to users after a Unity image deployment.

    Returns ``None`` (disabling the filter) if GCS is unreachable and no
    cached value is available.
    """
    global _cached_image_hash, _image_hash_fetched_at
    now = time.monotonic()
    if _cached_image_hash and (now - _image_hash_fetched_at) < _IMAGE_HASH_CACHE_TTL:
        return _cached_image_hash

    try:
        from google.cloud import storage  # deferred to avoid import-time cost

        client = storage.Client()
        blob = client.bucket(_IMAGE_HASH_BUCKET).blob(SETTINGS.image_hash_blob)
        content = blob.download_as_text()
        _cached_image_hash = content.strip()
        _image_hash_fetched_at = now
        logger.info("Refreshed current image hash: %s", _cached_image_hash)
        return _cached_image_hash
    except Exception:
        logger.warning(
            "Failed to read image hash from GCS; using cached value: %s",
            _cached_image_hash,
        )
        return _cached_image_hash


def _load_clients() -> None:
    global _batch_api, _core_api, _custom_api, _coord_api
    if _batch_api and _core_api and _custom_api and _coord_api:
        return
    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()
    api_client = k8s_client.ApiClient()
    _batch_api = k8s_client.BatchV1Api(api_client)
    _core_api = k8s_client.CoreV1Api(api_client)
    _custom_api = k8s_client.CustomObjectsApi(api_client)
    _coord_api = k8s_client.CoordinationV1Api(api_client)


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


def _vm_assignment_signal(body: dict) -> dict:
    return _signal_by_name(body, SIGNAL_VM_ASSIGNMENT)


def _desktop_ready_signal(body: dict) -> dict:
    return _signal_by_name(body, SIGNAL_DESKTOP_READY)


def _guest_health_signal(body: dict) -> dict:
    return _signal_by_name(body, SIGNAL_VM_GUEST_HEALTH)


def _release_request_signal(body: dict) -> dict:
    return _signal_by_name(body, SIGNAL_VM_RELEASE_REQUEST)


def _release_complete_signal(body: dict) -> dict:
    return _signal_by_name(body, SIGNAL_VM_RELEASE_COMPLETE)


def _remaining_signals(
    assistant_id: str,
    body: dict,
    *consumed_signal_names: str,
) -> dict:
    """Return latest signals minus any controller-consumed names.

    Reconcile may enqueue a background worker that records a fresh signal before
    the controller's own status patch executes. Reading the latest stored
    session here preserves those newly-written signals instead of replacing them
    with the stale signal set from the original reconcile snapshot.
    """

    assert _custom_api is not None
    current = get_assistant_session(_custom_api, WATCH_NAMESPACE, assistant_id) or body
    return _signals_without(current, *consumed_signal_names)


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


def _active_jobs_for_assistant(assistant_id: str) -> list:
    """Return active Jobs currently labeled for an assistant."""

    assert _batch_api is not None
    sanitized_assistant_id = _sanitize_for_k8s(assistant_id)
    jobs = _batch_api.list_namespaced_job(
        namespace=WATCH_NAMESPACE,
        label_selector=f"app=unity,assistant-id={sanitized_assistant_id}",
    )
    return [
        job
        for job in jobs.items
        if job.status.active
        and job.status.active > 0
        and not job.metadata.deletion_timestamp
    ]


def _suspend_extra_assistant_jobs(
    assistant_id: str,
    *,
    current_job_name: str | None,
    source: str,
) -> list[str]:
    """Best-effort suspend of non-authoritative live Jobs for an assistant."""

    suspended_jobs: list[str] = []
    for job in _active_jobs_for_assistant(assistant_id):
        job_name = str(job.metadata.name or "")
        if current_job_name and job_name == current_job_name:
            continue
        try:
            _suspend_bound_job(job, source=source)
        except ApiException:
            logger.exception(
                "Failed to suspend extra Job %s for assistant %s",
                job_name,
                assistant_id,
            )
            continue
        suspended_jobs.append(job_name)
    return suspended_jobs


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
    current_hash = _get_current_image_hash()

    label_selector = "app=unity,unity-status=idle"
    if current_hash:
        label_selector += f",unity-image-hash={current_hash}"

    jobs = _batch_api.list_namespaced_job(
        namespace=WATCH_NAMESPACE,
        label_selector=label_selector,
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
        reverse=True,
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


def _claim_and_bind_pending_job(
    *,
    assistant_id: str,
    session_name: str,
    activation_id: str,
    binding: dict,
    existing_conditions: list[dict],
    desktop_required: bool,
    bootstrap_retries: int,
    vm_retries: int,
) -> JobClaimTransitionResult:
    """Advance a PendingJob binding to PendingContainer under a single-flight lease.

    Returns one of:
    - ``claimed`` when the binding was persisted with a Job.
    - ``capacity`` when no idle Job was available.
    - ``busy`` when another reconcile currently owns the claim transition.
    """

    assert _coord_api is not None
    current_binding_id = binding_id_from_status(binding)
    if not current_binding_id:
        return _JOB_CLAIM_RESULT_CAPACITY

    holder_id = f"job-claim-{current_binding_id}-{uuid.uuid4().hex[:8]}"
    acquired = acquire_assignment_lease(
        _coord_api,
        assistant_id,
        WATCH_NAMESPACE,
        holder_id,
        duration=_JOB_CLAIM_LEASE_DURATION_SECONDS,
    )
    if not acquired:
        emit_observability_event(
            "controller.binding_job_claim_busy",
            assistant_id=assistant_id,
            session_name=session_name,
            binding_id=current_binding_id,
            source="controller.reconcile",
        )
        return _JOB_CLAIM_RESULT_BUSY

    try:
        job = _job_for_binding(session_name, binding)
        newly_claimed = False
        if job is None:
            job = _claim_idle_job_for_binding(assistant_id, session_name, binding)
            if job is None:
                return _JOB_CLAIM_RESULT_CAPACITY
            newly_claimed = True

        next_binding = _binding_payload(
            binding,
            job_ref={"name": job.metadata.name, "namespace": WATCH_NAMESPACE},
            pod_ref=_current_pod_ref(job.metadata.name),
            created_at=_now_iso(),
        )
        try:
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="PendingContainer",
                observed_activation_id=activation_id,
                binding=next_binding,
                last_error="",
                source="controller.reconcile",
                bootstrap_retries=bootstrap_retries,
                vm_retries=vm_retries,
                desktop_probe_failures=0,
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
        except ApiException as exc:
            if exc.status != 409:
                raise
            latest_session = (
                get_assistant_session(_custom_api, WATCH_NAMESPACE, assistant_id)
                if _custom_api is not None
                else None
            )
            latest_binding = session_binding(latest_session)
            latest_job_name = str(binding_job_ref(latest_binding).get("name", "") or "")
            if latest_job_name == str(job.metadata.name or ""):
                return _JOB_CLAIM_RESULT_CLAIMED
            if newly_claimed:
                _suspend_bound_job(job, source="controller.claim_conflict_cleanup")
            raise
        return _JOB_CLAIM_RESULT_CLAIMED
    finally:
        release_assignment_lease(_coord_api, assistant_id, WATCH_NAMESPACE)


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


def _stale_other_binding_release_candidate(
    other_runtime_vms: list[dict],
    disk_vm_name: str | None,
) -> dict | None:
    """Return a stale releasing VM that is safe to finalize for cleanup."""

    if len(other_runtime_vms) != 1:
        return None

    candidate = other_runtime_vms[0]
    candidate_vm_name = str(candidate.get("vm_name", "") or "")
    candidate_binding_id = str(candidate.get("binding_id", "") or "")
    candidate_role = str(candidate.get("pool_role", "") or "")
    if (
        not candidate_vm_name
        or not candidate_binding_id
        or candidate_role != POOL_ROLE_RELEASING
    ):
        return None
    if disk_vm_name not in (None, candidate_vm_name):
        return None
    return candidate


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
    body: dict,
    assistant_id: str,
    session_name: str,
    binding: dict,
    existing_conditions: list[dict],
    desktop_required: bool,
    source_reason: str,
) -> tuple[str, dict, list[dict], str]:
    """Drive release until the binding is fully cleaned up."""

    current_binding_id = binding_id_from_status(binding)
    current_job_name = str(binding_job_ref(binding).get("name", "") or "")
    job = _job_for_binding(session_name, binding)
    job_live = job is not None and _job_terminal_phase(job) is None
    vm_ref = binding_vm_ref(binding)
    vm_name = str(vm_ref.get("name", "") or "")
    release_requested_at = str(binding.get("releaseRequestedAt", "") or "")
    release_completed_at = str(binding.get("releaseCompletedAt", "") or "")
    release_request_signal = _release_request_signal(body)
    release_complete_signal = _release_complete_signal(body)
    last_error = ""

    if job_live:
        try:
            _suspend_bound_job(job, source=f"controller.release.{source_reason}")
        except ApiException as exc:  # pragma: no cover - best effort suspend
            logger.exception(
                "Failed to suspend Job %s during release",
                job.metadata.name,
            )
            last_error = str(exc)

    extra_live_job_names = _suspend_extra_assistant_jobs(
        assistant_id,
        current_job_name=current_job_name or None,
        source=f"controller.release.{source_reason}.extra_job",
    )
    if extra_live_job_names:
        emit_observability_event(
            "controller.release_state.suspend_extra_jobs",
            assistant_id=assistant_id,
            session_name=session_name,
            binding_id=current_binding_id,
            current_job_name=current_job_name or None,
            extra_job_names=extra_live_job_names,
            source_reason=source_reason,
        )

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

    stale_other_vm = None
    if not vm_name and not job_live and not owned_runtime_vms:
        stale_other_vm = _stale_other_binding_release_candidate(
            other_runtime_vms,
            disk_vm_name,
        )

    if vm_name:
        if not release_completed_at and _binding_signal_matches(
            release_complete_signal,
            current_binding_id,
        ):
            release_completed_at = str(
                release_complete_signal.get("observedAt", "") or _now_iso(),
            )
            binding = _binding_payload(
                binding,
                release_completed_at=release_completed_at,
            )
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
            request_signal_state = str(release_request_signal.get("state", "") or "")
            if _binding_signal_matches(
                release_request_signal,
                current_binding_id,
            ) and request_signal_state in {"requested", "retired"}:
                if not release_requested_at:
                    release_requested_at = str(
                        release_request_signal.get("observedAt", "") or _now_iso(),
                    )
                    binding = _binding_payload(
                        binding,
                        release_requested_at=release_requested_at,
                    )
            else:
                emit_observability_event(
                    "controller.release_state.queue_vm_release",
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
                schedule_vm_release_request(
                    custom_api=_custom_api,
                    namespace=WATCH_NAMESPACE,
                    assistant_id=assistant_id,
                    binding_id=current_binding_id,
                    vm_name=vm_name,
                )
            if (
                _binding_signal_matches(release_request_signal, current_binding_id)
                and request_signal_state == "retired"
            ):
                release_completed_at = release_requested_at or _now_iso()
                binding = _binding_payload(
                    binding,
                    vm_ref=None,
                    desktop_url=None,
                    release_requested_at=release_requested_at,
                    release_completed_at=release_completed_at,
                )
            elif _binding_signal_matches(release_request_signal, current_binding_id):
                request_error = str(release_request_signal.get("message", "") or "")
                if request_error:
                    last_error = request_error
    else:
        if stale_other_vm is not None:
            emit_observability_event(
                "controller.release_state.complete_other_binding_release",
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
                stale_vm_name=stale_other_vm["vm_name"],
                stale_binding_id=stale_other_vm["binding_id"],
            )
            result = complete_pool_vm_release(
                str(stale_other_vm["vm_name"]),
                str(stale_other_vm["binding_id"]),
            )
            emit_observability_event(
                "controller.release_state.complete_other_binding_release_result",
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
                release_requested_at = release_requested_at or _now_iso()
                owned_runtime_vms, other_runtime_vms, disk_vm_name = (
                    _owned_runtime_cleanup_state(
                        assistant_id,
                        current_binding_id,
                    )
                )
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
    remaining_assistant_jobs = _active_jobs_for_assistant(assistant_id)
    remaining_assistant_job_names = [
        str(job.metadata.name or "") for job in remaining_assistant_jobs
    ]
    release_complete = (
        bool(binding.get("releaseCompletedAt"))
        and not refreshed_job_live
        and not remaining_assistant_jobs
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
        assistant_live_job_names=remaining_assistant_job_names,
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


def _assistant_release_state_without_binding(
    *,
    assistant_id: str,
    existing_conditions: list[dict],
    desktop_required: bool,
    source_reason: str,
) -> tuple[str, list[dict], str]:
    """Drive assistant-wide cleanup when no authoritative binding remains."""

    last_error = ""
    suspended_jobs = _suspend_extra_assistant_jobs(
        assistant_id,
        current_job_name=None,
        source=f"controller.release.{source_reason}.no_binding_job",
    )
    runtime_vms, _, disk_vm_name = _owned_runtime_cleanup_state(assistant_id, None)

    for vm in runtime_vms:
        vm_name = str(vm.get("vm_name", "") or "")
        binding_id = str(vm.get("binding_id", "") or "")
        pool_role = str(vm.get("pool_role", "") or "")
        if not vm_name or not binding_id:
            continue
        try:
            if pool_role == POOL_ROLE_RELEASING:
                complete_pool_vm_release(vm_name, binding_id)
            else:
                release_pool_vm(assistant_id, binding_id, vm_name=vm_name)
        except (
            ApiException,
            GoogleAPICallError,
        ) as exc:  # pragma: no cover - best effort release retry
            logger.exception(
                "Failed to clean runtime VM %s for assistant %s without binding",
                vm_name,
                assistant_id,
            )
            last_error = str(exc)

    remaining_jobs = _active_jobs_for_assistant(assistant_id)
    remaining_runtime_vms, _, remaining_disk_vm_name = _owned_runtime_cleanup_state(
        assistant_id,
        None,
    )
    emit_observability_event(
        "controller.release_state.no_binding_result",
        assistant_id=assistant_id,
        source_reason=source_reason,
        suspended_job_names=suspended_jobs,
        remaining_job_names=[str(job.metadata.name or "") for job in remaining_jobs],
        remaining_runtime_vm_names=[
            str(vm.get("vm_name", "") or "") for vm in remaining_runtime_vms
        ],
        remaining_disk_vm_name=remaining_disk_vm_name,
    )
    if (
        not remaining_jobs
        and not remaining_runtime_vms
        and remaining_disk_vm_name is None
    ):
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
        return "Released", released_conditions, last_error

    releasing_conditions = _condition_state(
        existing_conditions,
        "Releasing",
        desktop_required,
        container_assigned=bool(remaining_jobs),
        container_ready=False,
        vm_assigned=bool(remaining_runtime_vms),
        desktop_ready=False,
        reason="ReleaseRequested",
        message="Runtime cleanup in progress",
    )
    return "Releasing", releasing_conditions, last_error


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
    session = SessionSnapshot.from_body(body)
    session_name = session.session_name
    assistant_id = session.assistant_id
    activation_id = session.activation_id
    desired_state = session.desired_state
    desktop_required = session.desktop_required
    desktop_mode = session.desktop_mode
    secret_name = session.secret_name
    observed_activation_id = session.observed_activation_id
    existing_conditions = session.existing_conditions
    binding = session.binding
    bootstrap_retries = session.bootstrap_retries
    vm_retries = session.vm_retries
    desktop_probe_failures = session.desktop_probe_failures
    persisted_last_error = session.last_error
    pending_binding_last_error = ""

    emit_observability_event(
        "controller.session_reconcile",
        **assistant_session_observability_fields(
            body,
            source="controller.reconcile",
        ),
        worker_stats=worker_runtime_stats(),
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

    current_binding_id = session.current_binding_id
    phase = session.phase

    if (
        desired_state != DESIRED_STATE_STOPPED
        and phase == "Failed"
        and not current_binding_id
        and observed_activation_id == activation_id
    ):
        return

    if desired_state == DESIRED_STATE_STOPPED:
        if not current_binding_id:
            release_phase, release_conditions, release_error = (
                _assistant_release_state_without_binding(
                    assistant_id=assistant_id,
                    existing_conditions=existing_conditions,
                    desktop_required=desktop_required,
                    source_reason="desired_stop_no_binding",
                )
            )
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase=release_phase,
                observed_activation_id=observed_activation_id or activation_id,
                binding=None,
                last_error=release_error if release_error else "",
                source="controller.reconcile",
                conditions=release_conditions,
            )
            return
        release_phase, release_binding, release_conditions, release_error = (
            _binding_release_state(
                body=body,
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
                body=body,
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
                body=body,
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
                last_error=release_error or persisted_last_error,
                source="controller.reconcile",
                conditions=release_conditions,
            )
            return
        pending_binding_last_error = persisted_last_error
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
            last_error=pending_binding_last_error,
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
                body=body,
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
                or "Recorded binding Job disappeared before runtime became ready",
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
        claim_result = _claim_and_bind_pending_job(
            assistant_id=assistant_id,
            session_name=session_name,
            activation_id=activation_id,
            binding=binding,
            existing_conditions=existing_conditions,
            desktop_required=desktop_required,
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
        )
        if claim_result == _JOB_CLAIM_RESULT_BUSY:
            return
        if claim_result == _JOB_CLAIM_RESULT_CAPACITY:
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
                body=body,
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
    assignment_signal = _vm_assignment_signal(body)
    if not current_vm_ref:
        if _binding_signal_matches(assignment_signal, current_binding_id):
            assignment_state = str(assignment_signal.get("state", "") or "")
            assignment_message = str(assignment_signal.get("message", "") or "")
            assignment_age = _signal_age_seconds(assignment_signal)
            if assignment_state == "assigned":
                signaled_vm_ref = assignment_signal.get("vmRef")
                if isinstance(signaled_vm_ref, dict) and signaled_vm_ref.get("name"):
                    binding = _binding_payload(
                        binding,
                        vm_ref=signaled_vm_ref,
                        vm_assigned_at=str(
                            assignment_signal.get("observedAt", "") or _now_iso(),
                        ),
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
                        signals=_remaining_signals(
                            assistant_id,
                            body,
                            SIGNAL_VM_ASSIGNMENT,
                        ),
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
            elif assignment_state in {"capacity", "waiting_release", "error"} and (
                assignment_age is None
                or assignment_age < VM_ASSIGNMENT_RETRY_INTERVAL_SECONDS
            ):
                reason = {
                    "capacity": "WaitingForCapacity",
                    "waiting_release": "WaitingForRelease",
                    "error": "AssignError",
                }[assignment_state]
                patch_assistant_session_status(
                    _custom_api,
                    WATCH_NAMESPACE,
                    assistant_id,
                    phase="PendingVM",
                    observed_activation_id=activation_id,
                    binding=binding,
                    last_error=assignment_message,
                    source="controller.reconcile",
                    conditions=_condition_state(
                        existing_conditions,
                        "PendingVM",
                        desktop_required,
                        container_assigned=True,
                        container_ready=True,
                        vm_assigned=False,
                        desktop_ready=False,
                        reason=reason,
                        message=assignment_message
                        or "Waiting for background VM assignment retry",
                    ),
                )
                return
        schedule_vm_assignment(
            custom_api=_custom_api,
            core_api=_core_api,
            namespace=WATCH_NAMESPACE,
            assistant_id=assistant_id,
            binding_id=current_binding_id,
            secret_name=secret_name,
            vm_type=desktop_mode or "ubuntu",
        )
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="PendingVM",
            observed_activation_id=activation_id,
            binding=binding,
            last_error="",
            source="controller.reconcile",
            conditions=_condition_state(
                existing_conditions,
                "PendingVM",
                desktop_required,
                container_assigned=True,
                container_ready=True,
                vm_assigned=False,
                desktop_ready=False,
                reason="AssignQueued",
                message="Queued VM assignment in background worker",
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
    consumed_signals: list[str] = []
    ready_signal = _desktop_ready_signal(body)
    if _binding_signal_matches(ready_signal, current_binding_id):
        signal_hostname = str(
            ready_signal.get("hostname", "") or verified_vm_ref.get("hostname", ""),
        )
        desktop_url = str(ready_signal.get("desktopUrl", "") or "")
        if not desktop_url and signal_hostname:
            desktop_url = f"https://{signal_hostname}"
        binding = _binding_payload(
            binding,
            desktop_url=desktop_url or None,
            vm_ready_observed_at=str(ready_signal.get("observedAt", "") or _now_iso()),
            vm_ready_hostname=signal_hostname or None,
            vm_ready_message_id=str(ready_signal.get("messageId", "") or ""),
        )
        consumed_signals.append(SIGNAL_DESKTOP_READY)

    desktop_url = binding_desktop_url(binding)
    desktop_ready_signal = bool(binding.get("vmReadyObservedAt")) and bool(desktop_url)
    guest_signal = _guest_health_signal(body)
    if _binding_signal_matches(guest_signal, current_binding_id):
        consumed_signals.append(SIGNAL_VM_GUEST_HEALTH)
        guest_state = str(guest_signal.get("state", "") or "")
        if guest_state == "ready":
            patch_kwargs = {}
            if consumed_signals:
                patch_kwargs["signals"] = _remaining_signals(
                    assistant_id,
                    body,
                    *consumed_signals,
                )
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
                **patch_kwargs,
            )
            return
        if guest_state == "failed":
            probe_failures = desktop_probe_failures + 1
            if probe_failures >= DESKTOP_LIVENESS_FAILURE_THRESHOLD:
                release_phase, release_binding, release_conditions, release_error = (
                    _binding_release_state(
                        body=body,
                        assistant_id=assistant_id,
                        session_name=session_name,
                        binding=binding,
                        existing_conditions=existing_conditions,
                        desktop_required=desktop_required,
                        source_reason="desktop_liveness_failed",
                    )
                )
                if release_phase != "Released":
                    patch_kwargs = {}
                    if consumed_signals:
                        patch_kwargs["signals"] = _remaining_signals(
                            assistant_id,
                            body,
                            *consumed_signals,
                        )
                    patch_assistant_session_status(
                        _custom_api,
                        WATCH_NAMESPACE,
                        assistant_id,
                        phase=release_phase,
                        observed_activation_id=activation_id,
                        binding=release_binding if release_binding else None,
                        last_error=release_error
                        or "Desktop VM became unreachable after readiness",
                        source="controller.reconcile",
                        bootstrap_retries=bootstrap_retries,
                        vm_retries=vm_retries + 1,
                        desktop_probe_failures=0,
                        conditions=release_conditions,
                        **patch_kwargs,
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
                patch_kwargs = {}
                if consumed_signals:
                    patch_kwargs["signals"] = _remaining_signals(
                        assistant_id,
                        body,
                        *consumed_signals,
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
                    **patch_kwargs,
                )
                return
            patch_kwargs = {}
            if consumed_signals:
                patch_kwargs["signals"] = _remaining_signals(
                    assistant_id,
                    body,
                    *consumed_signals,
                )
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
                **patch_kwargs,
            )
            return

    if desktop_ready_signal:
        schedule_guest_health_probe(
            custom_api=_custom_api,
            namespace=WATCH_NAMESPACE,
            assistant_id=assistant_id,
            binding_id=current_binding_id,
            vm_ref=verified_vm_ref,
        )
        patch_kwargs = {}
        if consumed_signals:
            patch_kwargs["signals"] = _remaining_signals(
                assistant_id,
                body,
                *consumed_signals,
            )
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="PendingGuest" if phase != "Active" else "Active",
            observed_activation_id=activation_id,
            binding=binding,
            last_error="",
            source="controller.reconcile",
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
            desktop_probe_failures=desktop_probe_failures,
            conditions=_condition_state(
                existing_conditions,
                "PendingGuest" if phase != "Active" else "Active",
                desktop_required,
                container_assigned=True,
                container_ready=True,
                vm_assigned=True,
                desktop_ready=phase == "Active",
                reason="WaitingForDesktop" if phase != "Active" else "Ready",
                message=(
                    "Waiting for authenticated desktop readiness"
                    if phase != "Active"
                    else "Desktop session active"
                ),
            ),
            **patch_kwargs,
        )
        return

    if _binding_deadline_exceeded(
        binding,
        "vmAssignedAt",
        VM_READINESS_DEADLINE_SECONDS,
    ):
        release_phase, release_binding, release_conditions, release_error = (
            _binding_release_state(
                body=body,
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
                last_error=release_error
                or (
                    f"VM did not become ready within "
                    f"{int(VM_READINESS_DEADLINE_SECONDS)}s"
                ),
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


def _session_delete_cleanup_complete(body: dict) -> bool:
    """Return whether a terminating session has released all runtime artifacts."""

    session_name = str(body.get("metadata", {}).get("name", "") or "")
    spec = body.get("spec", {})
    status = body.get("status", {})
    assistant_id = str(spec.get("assistantId", "") or "")
    if not assistant_id:
        return True

    binding = session_binding(body)
    current_binding_id = binding_id_from_status(binding)
    phase = str(status.get("phase", "") or "")

    try:
        job = _job_for_binding(session_name, binding) if current_binding_id else None
    except Exception:  # pragma: no cover - best effort retry on transient API errors
        logger.exception(
            "Failed to inspect bound Job while finalizing AssistantSession deletion",
        )
        return False

    job_live = job is not None and _job_terminal_phase(job) is None
    try:
        assistant_live_jobs = _active_jobs_for_assistant(assistant_id)
    except Exception:  # pragma: no cover - best effort retry on transient API errors
        logger.exception(
            "Failed to inspect assistant Jobs while finalizing AssistantSession deletion",
        )
        return False
    try:
        owned_runtime_vms, other_runtime_vms, disk_vm_name = (
            _owned_runtime_cleanup_state(
                assistant_id,
                current_binding_id or None,
            )
        )
    except Exception:  # pragma: no cover - best effort retry on transient API errors
        logger.exception(
            "Failed to inspect runtime VMs while finalizing AssistantSession deletion",
        )
        return False

    return (
        phase == "Released"
        and not current_binding_id
        and not job_live
        and not assistant_live_jobs
        and not owned_runtime_vms
        and not other_runtime_vms
        and disk_vm_name is None
    )


@kopf.on.delete(
    SETTINGS.assistant_session_group,
    SETTINGS.assistant_session_version,
    SETTINGS.assistant_session_plural,
)
def delete_session(body, **_):
    assert _core_api is not None
    latest_body = _refresh_session_snapshot(body)
    if latest_body is None:
        return

    session_name = str(latest_body.get("metadata", {}).get("name", "") or "")
    spec = latest_body.get("spec", {})
    assistant_id = str(spec.get("assistantId", "") or "")
    secret_name = str(spec.get("startupSecretRef", "") or "")

    if assistant_id:
        _update_status_for_session(latest_body)
        latest_body = _refresh_session_snapshot(latest_body)
        if latest_body is not None and not _session_delete_cleanup_complete(
            latest_body,
        ):
            raise kopf.TemporaryError(
                "AssistantSession runtime cleanup still in progress",
                delay=RECONCILE_INTERVAL_SECONDS,
            )

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
                raise kopf.TemporaryError(
                    "AssistantSession bootstrap secret cleanup failed",
                    delay=RECONCILE_INTERVAL_SECONDS,
                ) from e

    emit_observability_event(
        "controller.session_delete.finalized",
        assistant_id=assistant_id or None,
        session_name=session_name or None,
    )


@kopf.on.probe(id="health")
def health_probe(**_):
    return {"ts": _now_iso(), "namespace": WATCH_NAMESPACE}
