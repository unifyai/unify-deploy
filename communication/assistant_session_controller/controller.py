from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
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
from communication.infra.observability import (
    bind_causal_context,
    build_causal_context,
    child_causal_context,
    current_causal_context,
    signal_causal_context,
)
from communication.infra.assistant_sessions import (
    ACTIVATION_ID_ANNOTATION,
    assistant_session_observability_fields,
    BINDING_ID_ANNOTATION,
    BINDING_ID_LABEL,
    binding_desktop_url,
    binding_id as binding_id_from_status,
    binding_release_generation,
    binding_job_ref,
    binding_pod_ref,
    binding_vm_assignment,
    binding_vm_ref,
    bootstrap_secret_owned_by_session,
    claim_binding_vm_assignment_attempt,
    CONTAINER_READY_ANNOTATION,
    DESIRED_STATE_STOPPED,
    build_suspend_intent,
    patch_assistant_session_spec,
    SIGNAL_DESKTOP_READY,
    SIGNAL_VM_GUEST_HEALTH,
    SIGNAL_VM_RELEASE_COMPLETE,
    SIGNAL_VM_RELEASE_REQUEST,
    SESSION_REF_ANNOTATION,
    SESSION_REF_LABEL,
    session_binding,
    session_suspend_intent,
    SUSPEND_INTENT_REPLACE,
    SUSPEND_INTENT_STOP,
    SUSPEND_INTENT_UNKNOWN,
    suspend_intent_value,
    build_condition,
    emit_observability_event,
    get_assistant_session,
    merge_conditions,
    patch_assistant_session_status,
    record_released_binding,
    resolve_current_binding_vm_ref,
)
from communication.infra.vm_helpers import (
    MAX_RELEASE_GENERATION,
    POOL_RELEASE_TIMEOUT_SECONDS,
    complete_pool_vm_release,
    find_vm_with_disk,
    POOL_ROLE_RELEASING,
    recover_stuck_pool_vm_release,
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
    parse_iso_or_none as _parse_iso_or_none,
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
RELEASE_REQUEST_TIMEOUT_SECONDS = POOL_RELEASE_TIMEOUT_SECONDS

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
VM_ASSIGNMENT_IN_PROGRESS_TIMEOUT_SECONDS = (
    CONFIG.vm_assignment_in_progress_timeout_seconds
)
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


def _vm_readiness_deadline_seconds() -> float:
    """Return the shared guest-readiness deadline for all desktop modes."""

    return VM_READINESS_DEADLINE_SECONDS


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


def _read_bound_pod(pod_ref: dict | None):
    """Return the currently bound pod object when it still exists."""

    if not isinstance(pod_ref, dict):
        return None
    pod_name = str(pod_ref.get("name", "") or "")
    if not pod_name:
        return None
    namespace = str(pod_ref.get("namespace", "") or WATCH_NAMESPACE)
    assert _core_api is not None
    try:
        return _core_api.read_namespaced_pod(name=pod_name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def _pod_running_started_at(pod_ref: dict | None) -> str | None:
    """Return when the bound pod entered a runnable Running state."""

    pod = _read_bound_pod(pod_ref)
    status = getattr(pod, "status", None)
    if getattr(status, "phase", "") != "Running":
        return None
    for container_status in getattr(status, "container_statuses", None) or []:
        running_state = getattr(
            getattr(container_status, "state", None),
            "running",
            None,
        )
        started_at = getattr(running_state, "started_at", None)
        if started_at is not None:
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            return started_at.astimezone(timezone.utc).isoformat()
    started_at = getattr(status, "start_time", None)
    if started_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return started_at.astimezone(timezone.utc).isoformat()


def _refresh_session_snapshot(body: dict) -> dict | None:
    assert _custom_api is not None
    assistant_id = str((body.get("spec") or {}).get("assistantId", ""))
    if not assistant_id:
        return body
    return get_assistant_session(_custom_api, WATCH_NAMESPACE, assistant_id)


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


def _matched_signal(
    body: dict,
    current_binding_id: str,
    *signal_names: str,
) -> tuple[str | None, dict]:
    """Return the first named signal that still belongs to the current binding."""

    for signal_name in signal_names:
        signal = _signal_by_name(body, signal_name)
        if _binding_signal_matches(signal, current_binding_id):
            return signal_name, signal
    return None, {}


def _signal_observability_fields(
    signal_name: str | None,
    signal: dict | None,
    *,
    prefix: str = "signal",
) -> dict[str, object]:
    """Return structured fields describing a persisted binding-scoped signal."""

    if not signal_name or not isinstance(signal, dict):
        return {}
    causal_fields = signal_causal_context(signal)
    return {
        f"{prefix}_name": signal_name,
        f"{prefix}_binding_id": str(signal.get("bindingId", "") or "") or None,
        f"{prefix}_release_generation": _signal_release_generation(signal),
        f"{prefix}_state": str(signal.get("state", "") or "") or None,
        f"{prefix}_source": str(signal.get("source", "") or "") or None,
        f"{prefix}_observed_at": str(signal.get("observedAt", "") or "") or None,
        f"{prefix}_message": str(signal.get("message", "") or "") or None,
        f"{prefix}_caller": causal_fields.get("caller"),
        f"{prefix}_root_caller": causal_fields.get("root_caller"),
        f"{prefix}_operation_id": causal_fields.get("operation_id"),
        f"{prefix}_root_operation_id": causal_fields.get("root_operation_id"),
    }


def _signal_release_generation(signal: dict | None) -> int | None:
    """Return the release generation recorded on a persisted signal."""

    if not isinstance(signal, dict):
        return None
    try:
        generation = int(signal.get("releaseGeneration"))
    except (TypeError, ValueError):
        return None
    return generation if generation > 0 else None


@contextmanager
def _bind_signal_reconcile_context(
    signal_name: str | None,
    signal: dict | None,
    *,
    caller: str,
):
    """Bind a child causal context when reconcile is driven by a persisted signal."""

    if not signal_name or not isinstance(signal, dict):
        yield {}
        return
    parent_context = signal_causal_context(signal) or current_causal_context()
    with bind_causal_context(
        child_causal_context(
            caller=caller,
            parent=parent_context,
            reason=f"persisted_signal:{signal_name}",
        ),
    ):
        emit_observability_event(
            "controller.signal_consumed",
            caller=caller,
            **_signal_observability_fields(signal_name, signal),
        )
        yield parent_context


def _binding_stage_observability_fields(
    *,
    assistant_id: str,
    session_name: str,
    phase: str,
    binding: dict | None,
    desktop_required: bool,
    bootstrap_retries: int,
    vm_retries: int,
    job=None,
    pod_ref: dict | None = None,
) -> dict[str, object]:
    """Return consistent observability fields for binding bootstrap stages."""

    metadata = getattr(job, "metadata", None)
    annotations = dict(getattr(metadata, "annotations", None) or {})
    created_at = str((binding or {}).get("createdAt", "") or "")
    container_bootstrap_started_at = str(
        (binding or {}).get("containerBootstrapStartedAt", "") or "",
    )
    guest_handshake_started_at = str(
        (binding or {}).get("guestHandshakeStartedAt", "") or "",
    )
    created_at_dt = _parse_iso_or_none(created_at)
    container_bootstrap_started_at_dt = _parse_iso_or_none(
        container_bootstrap_started_at,
    )
    guest_handshake_started_at_dt = _parse_iso_or_none(guest_handshake_started_at)
    binding_age_seconds = None
    if created_at_dt is not None:
        binding_age_seconds = int(
            (datetime.now(timezone.utc) - created_at_dt).total_seconds(),
        )
    container_bootstrap_age_seconds = None
    if container_bootstrap_started_at_dt is not None:
        container_bootstrap_age_seconds = int(
            (
                datetime.now(timezone.utc) - container_bootstrap_started_at_dt
            ).total_seconds(),
        )
    guest_handshake_age_seconds = None
    if guest_handshake_started_at_dt is not None:
        guest_handshake_age_seconds = int(
            (
                datetime.now(timezone.utc) - guest_handshake_started_at_dt
            ).total_seconds(),
        )
    resolved_pod_ref = (
        pod_ref if isinstance(pod_ref, dict) else binding_pod_ref(binding)
    )
    return {
        "assistant_id": assistant_id,
        "session_name": session_name,
        "phase": phase,
        "binding_id": binding_id_from_status(binding),
        "job_name": str(
            getattr(metadata, "name", "") or binding_job_ref(binding).get("name") or "",
        )
        or None,
        "pod_name": str((resolved_pod_ref or {}).get("name", "") or "") or None,
        "desktop_required": desktop_required,
        "bootstrap_retries": bootstrap_retries,
        "vm_retries": vm_retries,
        "binding_created_at": created_at or None,
        "binding_age_seconds": binding_age_seconds,
        "container_bootstrap_started_at": container_bootstrap_started_at or None,
        "container_bootstrap_age_seconds": container_bootstrap_age_seconds,
        "container_ready_at": str((binding or {}).get("containerReadyAt", "") or "")
        or None,
        "guest_handshake_started_at": guest_handshake_started_at or None,
        "guest_handshake_age_seconds": guest_handshake_age_seconds,
        "container_ready_annotation": annotations.get(CONTAINER_READY_ANNOTATION)
        or None,
    }


def _emit_binding_stage_event(
    event: str,
    *,
    assistant_id: str,
    session_name: str,
    phase: str,
    binding: dict | None,
    desktop_required: bool,
    bootstrap_retries: int,
    vm_retries: int,
    job=None,
    pod_ref: dict | None = None,
    **fields: object,
) -> None:
    """Emit a structured binding bootstrap stage event."""

    emit_observability_event(
        event,
        **_binding_stage_observability_fields(
            assistant_id=assistant_id,
            session_name=session_name,
            phase=phase,
            binding=binding,
            desktop_required=desktop_required,
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
            job=job,
            pod_ref=pod_ref,
        ),
        **fields,
    )


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
            binding_id = str(
                (job.metadata.labels or {}).get(BINDING_ID_LABEL, "") or "",
            )
            _suspend_bound_job(
                job,
                assistant_id=assistant_id,
                binding_id=binding_id,
                source=source,
                intent=SUSPEND_INTENT_REPLACE,
                source_reason="extra_job_cleanup",
            )
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
        emit_observability_event(
            "controller.pending_job_stage",
            assistant_id=assistant_id,
            session_name=session_name,
            binding_id=current_binding_id,
            job_name=existing_job.metadata.name,
            stage="reuse_existing_job",
            stage_state="completed",
            source="controller.reconcile",
        )
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
    )
    emit_observability_event(
        "controller.pending_job_stage",
        assistant_id=assistant_id,
        session_name=session_name,
        binding_id=current_binding_id,
        stage="scan_idle_jobs",
        stage_state="completed",
        source="controller.reconcile",
        label_selector=label_selector,
        current_image_hash=current_hash or None,
        idle_job_candidates=len(idle_jobs),
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
                emit_observability_event(
                    "controller.pending_job_stage",
                    assistant_id=assistant_id,
                    session_name=session_name,
                    binding_id=current_binding_id,
                    job_name=job.metadata.name,
                    stage="claim_idle_job",
                    stage_state="conflict",
                    source="controller.reconcile",
                )
                existing_job = _job_for_binding(session_name, binding)
                if existing_job is not None:
                    return existing_job
                continue
            raise

        emit_observability_event(
            "controller.pending_job_stage",
            assistant_id=assistant_id,
            session_name=session_name,
            binding_id=current_binding_id,
            job_name=job.metadata.name,
            stage="claim_idle_job",
            stage_state="completed",
            source="controller.reconcile",
        )
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

    emit_observability_event(
        "controller.pending_job_stage",
        assistant_id=assistant_id,
        session_name=session_name,
        binding_id=current_binding_id,
        stage="waiting_for_capacity",
        stage_state="blocked",
        source="controller.reconcile",
        label_selector=label_selector,
        current_image_hash=current_hash or None,
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

    _emit_binding_stage_event(
        "controller.pending_job_stage",
        assistant_id=assistant_id,
        session_name=session_name,
        phase="PendingJob",
        binding=binding,
        desktop_required=desktop_required,
        bootstrap_retries=bootstrap_retries,
        vm_retries=vm_retries,
        stage="claim_lease",
        stage_state="acquired",
        holder_id=holder_id,
    )
    try:
        job = _job_for_binding(session_name, binding)
        newly_claimed = False
        if job is None:
            job = _claim_idle_job_for_binding(assistant_id, session_name, binding)
            if job is None:
                return _JOB_CLAIM_RESULT_CAPACITY
            newly_claimed = True

        claim_origin = "claimed_idle" if newly_claimed else "existing_binding_job"
        pod_ref = _current_pod_ref(job.metadata.name)
        next_binding = _binding_payload(
            binding,
            job_ref={"name": job.metadata.name, "namespace": WATCH_NAMESPACE},
            pod_ref=pod_ref,
            container_bootstrap_started_at=(
                _now_iso() if _pod_running_started_at(pod_ref) else None
            ),
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
            _emit_binding_stage_event(
                "controller.pending_job_stage",
                assistant_id=assistant_id,
                session_name=session_name,
                phase="PendingContainer",
                binding=next_binding,
                desktop_required=desktop_required,
                bootstrap_retries=bootstrap_retries,
                vm_retries=vm_retries,
                job=job,
                stage="persist_pending_container",
                stage_state="completed",
                claim_origin=claim_origin,
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
            _emit_binding_stage_event(
                "controller.pending_job_stage",
                assistant_id=assistant_id,
                session_name=session_name,
                phase="PendingContainer",
                binding=next_binding,
                desktop_required=desktop_required,
                bootstrap_retries=bootstrap_retries,
                vm_retries=vm_retries,
                job=job,
                stage="persist_pending_container",
                stage_state="conflict",
                claim_origin=claim_origin,
                latest_job_name=latest_job_name or None,
            )
            if latest_job_name == str(job.metadata.name or ""):
                return _JOB_CLAIM_RESULT_CLAIMED
            if newly_claimed:
                _suspend_bound_job(
                    job,
                    assistant_id=assistant_id,
                    binding_id=current_binding_id,
                    source="controller.claim_conflict_cleanup",
                    intent=SUSPEND_INTENT_REPLACE,
                    source_reason="claim_conflict_cleanup",
                )
            raise
        return _JOB_CLAIM_RESULT_CLAIMED
    finally:
        release_assignment_lease(
            _coord_api,
            assistant_id,
            WATCH_NAMESPACE,
            holder_id,
        )


def _release_suspend_intent(source_reason: str) -> str:
    """Return the persisted suspend intent for a release-driven suspend."""

    if source_reason.startswith("desired_stop"):
        return SUSPEND_INTENT_STOP
    return SUSPEND_INTENT_REPLACE


def _record_binding_suspend_intent(
    *,
    assistant_id: str,
    binding_id: str,
    job_name: str,
    intent: str,
    source: str,
    source_reason: str | None = None,
) -> None:
    """Persist one binding-scoped suspend intent before the Job is suspended."""

    if _custom_api is None or not binding_id:
        return
    try:
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            expected_binding_id=binding_id,
            suspend_intent=build_suspend_intent(
                binding_id=binding_id,
                intent=intent,
                source=source,
                source_reason=source_reason,
                job_name=job_name,
            ),
            source=source,
        )
    except Exception:  # pragma: no cover - best effort status breadcrumb
        logger.exception(
            "Failed to record suspend intent for binding %s before suspending %s",
            binding_id,
            job_name,
        )
        emit_observability_event(
            "controller.binding_suspend_intent_record_failed",
            assistant_id=assistant_id,
            binding_id=binding_id,
            job_name=job_name,
            source=source,
            suspend_intent=intent,
            source_reason=source_reason,
        )


def _suspend_bound_job(
    job,
    *,
    assistant_id: str,
    binding_id: str,
    source: str,
    intent: str,
    source_reason: str | None = None,
) -> None:
    """Stop a binding-owned Job without mutating binding ownership."""

    assert _batch_api is not None
    _record_binding_suspend_intent(
        assistant_id=assistant_id,
        binding_id=binding_id,
        job_name=str(job.metadata.name or ""),
        intent=intent,
        source=source,
        source_reason=source_reason,
    )
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
        assistant_id=assistant_id,
        binding_id=binding_id,
        job_name=job.metadata.name,
        source=source,
        suspend_intent=intent,
        source_reason=source_reason,
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


def _current_binding_release_completion_candidate(
    owned_runtime_vms: list[dict],
    disk_vm_name: str | None,
) -> dict | None:
    """Return the current-binding releasing VM that is safe to finalize."""

    if len(owned_runtime_vms) != 1:
        return None

    candidate = owned_runtime_vms[0]
    candidate_vm_name = str(candidate.get("vm_name", "") or "")
    candidate_role = str(candidate.get("pool_role", "") or "")
    if not candidate_vm_name or candidate_role != POOL_ROLE_RELEASING:
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
        "release_generation": binding_release_generation(binding) or None,
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


def _recover_timed_out_release_request(
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
) -> tuple[dict, str, str, str]:
    """Recover a timed-out release by re-arming once, then retiring the VM."""

    current_binding_id = binding_id_from_status(binding)
    vm_name = str(binding_vm_ref(binding).get("name", "") or "")
    if not current_binding_id or not vm_name:
        return binding, release_requested_at, release_completed_at, ""

    current_release_generation = binding_release_generation(binding)
    allow_rearm = current_release_generation < MAX_RELEASE_GENERATION
    emit_observability_event(
        "controller.release_state.timeout_recovery",
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
        recovery_action="rearm" if allow_rearm else "retire",
        recovery_vm_name=vm_name,
    )
    result = recover_stuck_pool_vm_release(
        assistant_id,
        current_binding_id,
        vm_name=vm_name,
        current_release_generation=current_release_generation or None,
        allow_rearm=allow_rearm,
        retire_reason=f"controller_{source_reason}_release_timeout",
    )
    emit_observability_event(
        "controller.release_state.timeout_recovery_result",
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
        recovery_action=result.get("action"),
        recovery_result=result,
    )

    next_release_generation = current_release_generation
    try:
        next_release_generation = int(result.get("release_generation"))
    except (TypeError, ValueError):
        pass

    if result.get("retired"):
        release_requested_at = release_requested_at or _now_iso()
        release_completed_at = release_requested_at
        binding = _binding_payload(
            binding,
            vm_ref=None,
            desktop_url=None,
            release_requested_at=release_requested_at,
            release_completed_at=release_completed_at,
            release_generation=next_release_generation
            or current_release_generation
            or None,
        )
    elif result.get("action") == "rearmed":
        release_requested_at = _now_iso()
        release_completed_at = ""
        binding = _binding_payload(
            binding,
            release_requested_at=release_requested_at,
            release_completed_at=None,
            release_generation=next_release_generation
            or current_release_generation + 1,
        )

    message = str(result.get("message", "") or result.get("reason", "") or "")
    return binding, release_requested_at, release_completed_at, message


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
    release_generation = binding_release_generation(binding)
    release_request_signal = _release_request_signal(body)
    release_complete_signal = _release_complete_signal(body)
    last_error = ""

    if release_generation <= 0:
        release_generation = (
            _signal_release_generation(release_request_signal)
            or _signal_release_generation(release_complete_signal)
            or 0
        )
        if release_generation > 0:
            binding = _binding_payload(
                binding,
                release_generation=release_generation,
            )

    if job_live:
        try:
            _suspend_bound_job(
                job,
                assistant_id=assistant_id,
                binding_id=current_binding_id,
                source=f"controller.release.{source_reason}",
                intent=_release_suspend_intent(source_reason),
                source_reason=source_reason,
            )
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
    if not vm_name:
        resolved_vm_ref = resolve_current_binding_vm_ref(
            binding,
            owned_runtime_vms=owned_runtime_vms,
            disk_vm_name=disk_vm_name,
        )
        resolved_vm_name = str(resolved_vm_ref.get("name", "") or "")
        if resolved_vm_name:
            binding = _binding_payload(binding, vm_ref=resolved_vm_ref)
            vm_ref = resolved_vm_ref
            vm_name = resolved_vm_name
    if binding_vm_assignment(binding):
        binding = _binding_payload(binding, vm_assignment=None)
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

    if not release_completed_at and _binding_signal_matches(
        release_complete_signal,
        current_binding_id,
        release_generation=release_generation or None,
    ):
        release_completed_at = str(
            release_complete_signal.get("observedAt", "") or _now_iso(),
        )
        binding = _binding_payload(
            binding,
            release_completed_at=release_completed_at,
        )

    release_completion_vm_name = vm_name
    if not release_completion_vm_name and release_completed_at:
        completion_candidate = _current_binding_release_completion_candidate(
            owned_runtime_vms,
            disk_vm_name,
        )
        if completion_candidate is not None:
            release_completion_vm_name = str(
                completion_candidate.get("vm_name", "") or "",
            )

    stale_other_vm = None
    if not vm_name and not job_live and not owned_runtime_vms:
        stale_other_vm = _stale_other_binding_release_candidate(
            other_runtime_vms,
            disk_vm_name,
        )

    if vm_name or release_completion_vm_name:
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
                completion_vm_name=release_completion_vm_name or None,
            )
            result = complete_pool_vm_release(
                release_completion_vm_name,
                current_binding_id,
            )
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
                completion_vm_name=release_completion_vm_name or None,
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
            request_signal_matches = _binding_signal_matches(
                release_request_signal,
                current_binding_id,
                release_generation=release_generation or None,
            )
            if request_signal_matches and request_signal_state in {
                "requested",
                "retired",
            }:
                if not release_requested_at:
                    release_requested_at = str(
                        release_request_signal.get("observedAt", "") or _now_iso(),
                    )
                if not release_generation:
                    release_generation = (
                        _signal_release_generation(
                            release_request_signal,
                        )
                        or 0
                    )
                binding = _binding_payload(
                    binding,
                    release_requested_at=release_requested_at,
                    release_generation=release_generation or None,
                )
            if request_signal_matches and request_signal_state == "retired":
                release_completed_at = release_requested_at or _now_iso()
                binding = _binding_payload(
                    binding,
                    vm_ref=None,
                    desktop_url=None,
                    release_requested_at=release_requested_at,
                    release_completed_at=release_completed_at,
                    release_generation=release_generation or None,
                )
            else:
                request_timed_out = bool(
                    release_requested_at,
                ) and _binding_deadline_exceeded(
                    _binding_payload(
                        binding,
                        release_requested_at=release_requested_at,
                        release_generation=release_generation or None,
                    ),
                    "releaseRequestedAt",
                    RELEASE_REQUEST_TIMEOUT_SECONDS,
                )
                if request_timed_out:
                    (
                        binding,
                        release_requested_at,
                        release_completed_at,
                        recovery_error,
                    ) = _recover_timed_out_release_request(
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
                    )
                    release_generation = binding_release_generation(binding)
                    if recovery_error:
                        last_error = recovery_error
                elif not release_requested_at or release_generation <= 0:
                    release_retry = bool(release_requested_at)
                    release_requested_at = release_requested_at or _now_iso()
                    release_generation = release_generation or 1
                    binding = _binding_payload(
                        binding,
                        release_requested_at=release_requested_at,
                        release_completed_at=None,
                        release_generation=release_generation,
                    )
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
                        release_retry=release_retry,
                    )
                    schedule_vm_release_request(
                        custom_api=_custom_api,
                        namespace=WATCH_NAMESPACE,
                        assistant_id=assistant_id,
                        binding_id=current_binding_id,
                        vm_name=vm_name,
                        release_generation=release_generation,
                    )
            if request_signal_matches:
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
        if current_binding_id and release_completed_at:
            record_released_binding(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                binding_id=current_binding_id,
                release_requested_at=release_requested_at or None,
                release_completed_at=release_completed_at,
                source=f"controller.release.{source_reason}",
            )
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


def _recover_failed_running_session_without_binding(
    *,
    body: dict,
    assistant_id: str,
    activation_id: str,
    existing_conditions: list[dict],
    desktop_required: bool,
    bootstrap_retries: int,
    vm_retries: int,
    last_error: str,
) -> None:
    """Recover or stop a failed running session that no longer has a binding."""

    release_phase, release_conditions, release_error = (
        _assistant_release_state_without_binding(
            assistant_id=assistant_id,
            existing_conditions=existing_conditions,
            desktop_required=desktop_required,
            source_reason="failed_running_no_binding",
        )
    )
    persisted_suspend_intent = suspend_intent_value(session_suspend_intent(body))
    if release_phase != "Released":
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase=release_phase,
            observed_activation_id=activation_id,
            binding=None,
            last_error=release_error if release_error else last_error,
            source="controller.reconcile",
            conditions=release_conditions,
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
        )
        return

    if persisted_suspend_intent == SUSPEND_INTENT_STOP:
        patch_assistant_session_spec(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            desired_state=DESIRED_STATE_STOPPED,
        )
        emit_observability_event(
            "controller.failed_running_no_binding_stopped",
            assistant_id=assistant_id,
            activation_id=activation_id,
            suspend_intent=persisted_suspend_intent,
        )
        return

    patch_assistant_session_status(
        _custom_api,
        WATCH_NAMESPACE,
        assistant_id,
        phase="PendingJob",
        observed_activation_id=activation_id,
        binding=_mint_binding_payload(),
        last_error=release_error or last_error,
        source="controller.reconcile",
        bootstrap_retries=bootstrap_retries,
        vm_retries=vm_retries,
        desktop_probe_failures=0,
        conditions=_condition_state(
            release_conditions,
            "PendingJob",
            desktop_required,
            container_assigned=False,
            container_ready=False,
            vm_assigned=False,
            desktop_ready=False,
            reason="RecoveredFailedState",
            message="Recovered failed running session without a binding",
        ),
    )
    emit_observability_event(
        "controller.failed_running_no_binding_recovered",
        assistant_id=assistant_id,
        activation_id=activation_id,
        suspend_intent=persisted_suspend_intent or SUSPEND_INTENT_UNKNOWN,
    )


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
    current_binding_id = session.current_binding_id
    phase = session.phase
    reconcile_signal_name, reconcile_signal = _matched_signal(
        body,
        current_binding_id,
        SIGNAL_VM_RELEASE_COMPLETE,
        SIGNAL_VM_RELEASE_REQUEST,
        SIGNAL_VM_GUEST_HEALTH,
        SIGNAL_DESKTOP_READY,
    )
    continued_signal_parent_context: dict | None = None

    emit_observability_event(
        "controller.session_reconcile",
        **assistant_session_observability_fields(
            body,
            source="controller.reconcile",
        ),
        **_signal_observability_fields(
            reconcile_signal_name,
            reconcile_signal,
            prefix="matched_signal",
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
        release_signal_name, release_signal = _matched_signal(
            body,
            current_binding_id,
            SIGNAL_VM_RELEASE_COMPLETE,
            SIGNAL_VM_RELEASE_REQUEST,
        )
        with _bind_signal_reconcile_context(
            release_signal_name,
            release_signal,
            caller="controller.reconcile.release_state",
        ):
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
        _recover_failed_running_session_without_binding(
            body=body,
            assistant_id=assistant_id,
            activation_id=activation_id,
            existing_conditions=existing_conditions,
            desktop_required=desktop_required,
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
            last_error=persisted_last_error,
        )
        return

    if (
        current_binding_id
        and observed_activation_id
        and observed_activation_id != activation_id
    ):
        release_signal_name, release_signal = _matched_signal(
            body,
            current_binding_id,
            SIGNAL_VM_RELEASE_COMPLETE,
            SIGNAL_VM_RELEASE_REQUEST,
        )
        with _bind_signal_reconcile_context(
            release_signal_name,
            release_signal,
            caller="controller.reconcile.release_state",
        ) as release_parent_context:
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
            if release_parent_context:
                continued_signal_parent_context = release_parent_context
        binding = {}
        current_binding_id = ""
        existing_conditions = release_conditions
        phase = "Released"

    if phase == "Releasing" and current_binding_id:
        release_signal_name, release_signal = _matched_signal(
            body,
            current_binding_id,
            SIGNAL_VM_RELEASE_COMPLETE,
            SIGNAL_VM_RELEASE_REQUEST,
        )
        with _bind_signal_reconcile_context(
            release_signal_name,
            release_signal,
            caller="controller.reconcile.release_state",
        ) as release_parent_context:
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
            if release_parent_context:
                continued_signal_parent_context = release_parent_context
        pending_binding_last_error = persisted_last_error
        binding = {}
        current_binding_id = ""
        existing_conditions = release_conditions
        phase = "Released"

    if not current_binding_id:
        next_binding = _mint_binding_payload()
        pending_job_context = (
            bind_causal_context(
                child_causal_context(
                    caller="controller.reconcile.pending_job",
                    parent=continued_signal_parent_context,
                    reason="post_release_signal",
                ),
            )
            if continued_signal_parent_context
            else nullcontext()
        )
        with pending_job_context:
            _emit_binding_stage_event(
                "controller.pending_job_stage",
                assistant_id=assistant_id,
                session_name=session_name,
                phase="PendingJob",
                binding=next_binding,
                desktop_required=desktop_required,
                bootstrap_retries=bootstrap_retries,
                vm_retries=vm_retries,
                stage="mint_binding",
                stage_state="completed",
                previous_phase=phase or None,
                carried_signal_context=bool(continued_signal_parent_context),
            )
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="PendingJob",
                observed_activation_id=activation_id,
                binding=next_binding,
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
        _emit_binding_stage_event(
            "controller.pending_container_stage",
            assistant_id=assistant_id,
            session_name=session_name,
            phase=phase or "PendingContainer",
            binding=binding,
            desktop_required=desktop_required,
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
            stage="job_missing",
            stage_state="failed",
        )
        release_signal_name, release_signal = _matched_signal(
            body,
            current_binding_id,
            SIGNAL_VM_RELEASE_COMPLETE,
            SIGNAL_VM_RELEASE_REQUEST,
        )
        with _bind_signal_reconcile_context(
            release_signal_name,
            release_signal,
            caller="controller.reconcile.release_state",
        ):
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
            _emit_binding_stage_event(
                "controller.pending_job_stage",
                assistant_id=assistant_id,
                session_name=session_name,
                phase="PendingJob",
                binding=binding,
                desktop_required=desktop_required,
                bootstrap_retries=bootstrap_retries,
                vm_retries=vm_retries,
                stage="waiting_for_capacity",
                stage_state="blocked",
            )
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
        binding_suspend_intent = suspend_intent_value(
            session_suspend_intent(body),
            binding_id=current_binding_id,
        )
        _emit_binding_stage_event(
            "controller.pending_container_stage",
            assistant_id=assistant_id,
            session_name=session_name,
            phase=phase or "PendingContainer",
            binding=binding,
            desktop_required=desktop_required,
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
            job=job,
            pod_ref=pod_ref,
            stage="job_terminal",
            stage_state="failed",
            terminal_phase=terminal_phase,
        )
        release_signal_name, release_signal = _matched_signal(
            body,
            current_binding_id,
            SIGNAL_VM_RELEASE_COMPLETE,
            SIGNAL_VM_RELEASE_REQUEST,
        )
        with _bind_signal_reconcile_context(
            release_signal_name,
            release_signal,
            caller="controller.reconcile.release_state",
        ):
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
            if (
                binding_suspend_intent == SUSPEND_INTENT_STOP
                and desired_state != DESIRED_STATE_STOPPED
            ):
                patch_assistant_session_spec(
                    _custom_api,
                    WATCH_NAMESPACE,
                    assistant_id,
                    desired_state=DESIRED_STATE_STOPPED,
                )
                emit_observability_event(
                    "controller.pending_container_terminal_job_stop_intent",
                    assistant_id=assistant_id,
                    activation_id=activation_id,
                    binding_id=current_binding_id,
                    job_name=job.metadata.name,
                    terminal_phase=terminal_phase,
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

    if not binding.get("containerBootstrapStartedAt"):
        container_bootstrap_started_at = _pod_running_started_at(pod_ref)
        if container_bootstrap_started_at:
            binding = _binding_payload(
                binding,
                container_bootstrap_started_at=container_bootstrap_started_at,
            )

    container_ready = (job.metadata.annotations or {}).get(
        CONTAINER_READY_ANNOTATION,
    ) == "true"
    if not container_ready:
        if not binding.get("containerBootstrapStartedAt"):
            _emit_binding_stage_event(
                "controller.pending_container_stage",
                assistant_id=assistant_id,
                session_name=session_name,
                phase="PendingContainer",
                binding=binding,
                desktop_required=desktop_required,
                bootstrap_retries=bootstrap_retries,
                vm_retries=vm_retries,
                job=job,
                pod_ref=pod_ref,
                stage="pod_running_wait",
                stage_state="pending",
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
                    reason="WaitingForPodStart",
                    message="Waiting for bound Unity pod to enter Running state",
                ),
            )
            return
        if _binding_deadline_exceeded(
            binding,
            "containerBootstrapStartedAt",
            CONTAINER_BOOTSTRAP_DEADLINE_SECONDS,
        ):
            _emit_binding_stage_event(
                "controller.pending_container_stage",
                assistant_id=assistant_id,
                session_name=session_name,
                phase="PendingContainer",
                binding=binding,
                desktop_required=desktop_required,
                bootstrap_retries=bootstrap_retries,
                vm_retries=vm_retries,
                job=job,
                pod_ref=pod_ref,
                stage="container_ready_wait",
                stage_state="timeout",
                deadline_seconds=CONTAINER_BOOTSTRAP_DEADLINE_SECONDS,
            )
            try:
                _suspend_bound_job(
                    job,
                    assistant_id=assistant_id,
                    binding_id=current_binding_id,
                    source="controller.bootstrap_timeout",
                    intent=SUSPEND_INTENT_REPLACE,
                    source_reason="bootstrap_timeout",
                )
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
        _emit_binding_stage_event(
            "controller.pending_container_stage",
            assistant_id=assistant_id,
            session_name=session_name,
            phase="PendingContainer",
            binding=binding,
            desktop_required=desktop_required,
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
            job=job,
            pod_ref=pod_ref,
            stage="container_ready_wait",
            stage_state="pending",
            deadline_seconds=CONTAINER_BOOTSTRAP_DEADLINE_SECONDS,
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
                message="Unity has not yet signaled container-ready",
            ),
        )
        return

    if not binding.get("containerReadyAt"):
        _emit_binding_stage_event(
            "controller.pending_container_stage",
            assistant_id=assistant_id,
            session_name=session_name,
            phase="PendingContainer",
            binding=binding,
            desktop_required=desktop_required,
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
            job=job,
            pod_ref=pod_ref,
            stage="container_ready_observed",
            stage_state="completed",
        )
        binding = _binding_payload(
            binding,
            container_ready_at=_now_iso(),
        )

    if not desktop_required:
        _emit_binding_stage_event(
            "controller.pending_container_stage",
            assistant_id=assistant_id,
            session_name=session_name,
            phase="Active",
            binding=binding,
            desktop_required=desktop_required,
            bootstrap_retries=bootstrap_retries,
            vm_retries=vm_retries,
            job=job,
            pod_ref=pod_ref,
            stage="container_active",
            stage_state="completed",
        )
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
        assignment = binding_vm_assignment(binding)
        assignment_state = str(assignment.get("state", "") or "")
        assignment_message = str(assignment.get("message", "") or "")
        assignment_age = _signal_age_seconds(assignment)
        if assignment_state == "in_progress" and (
            assignment_age is None
            or assignment_age < VM_ASSIGNMENT_IN_PROGRESS_TIMEOUT_SECONDS
        ):
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="PendingVM",
                observed_activation_id=activation_id,
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
        if assignment_state in {"capacity", "waiting_release", "error"} and (
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
        attempt_id = claim_binding_vm_assignment_attempt(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            target_binding_id=current_binding_id,
            stale_after_seconds=VM_ASSIGNMENT_IN_PROGRESS_TIMEOUT_SECONDS,
            source="controller.reconcile",
        )
        if attempt_id is None:
            return
        schedule_vm_assignment(
            custom_api=_custom_api,
            core_api=_core_api,
            namespace=WATCH_NAMESPACE,
            assistant_id=assistant_id,
            binding_id=current_binding_id,
            attempt_id=attempt_id,
            secret_name=secret_name,
            vm_type=desktop_mode or "ubuntu",
        )
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="PendingVM",
            observed_activation_id=activation_id,
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

    if binding_vm_assignment(binding):
        binding = _binding_payload(binding, vm_assignment=None)
    verified_vm_ref = verify_vm_assignment(
        str(current_vm_ref.get("name", "") or ""),
        current_binding_id,
        assistant_id,
    )
    if verified_vm_ref is None:
        try:
            _suspend_bound_job(
                job,
                assistant_id=assistant_id,
                binding_id=current_binding_id,
                source="controller.vm_ownership_lost",
                intent=SUSPEND_INTENT_REPLACE,
                source_reason="vm_ownership_lost",
            )
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
    if not binding.get("guestHandshakeStartedAt"):
        binding = _binding_payload(
            binding,
            guest_handshake_started_at=(
                str(binding.get("vmAssignedAt", "") or "") or _now_iso()
            ),
        )
    consumed_signals: list[str] = []
    ready_signal_parent_context: dict | None = None
    ready_signal = _desktop_ready_signal(body)
    if _binding_signal_matches(ready_signal, current_binding_id):
        with _bind_signal_reconcile_context(
            SIGNAL_DESKTOP_READY,
            ready_signal,
            caller="controller.reconcile.desktop_ready",
        ) as current_ready_signal_parent_context:
            signal_hostname = str(
                ready_signal.get("hostname", "") or verified_vm_ref.get("hostname", ""),
            )
            desktop_url = str(ready_signal.get("desktopUrl", "") or "")
            if not desktop_url and signal_hostname:
                desktop_url = f"https://{signal_hostname}"
            binding = _binding_payload(
                binding,
                desktop_url=desktop_url or None,
                vm_ready_observed_at=str(
                    ready_signal.get("observedAt", "") or _now_iso(),
                ),
                vm_ready_hostname=signal_hostname or None,
                vm_ready_message_id=str(ready_signal.get("messageId", "") or ""),
            )
            consumed_signals.append(SIGNAL_DESKTOP_READY)
            if current_ready_signal_parent_context:
                ready_signal_parent_context = current_ready_signal_parent_context

    desktop_url = binding_desktop_url(binding)
    desktop_ready_signal = bool(binding.get("vmReadyObservedAt")) and bool(desktop_url)
    guest_signal = _guest_health_signal(body)
    if _binding_signal_matches(guest_signal, current_binding_id):
        with _bind_signal_reconcile_context(
            SIGNAL_VM_GUEST_HEALTH,
            guest_signal,
            caller="controller.reconcile.vm_guest_health",
        ):
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
                    (
                        release_phase,
                        release_binding,
                        release_conditions,
                        release_error,
                    ) = _binding_release_state(
                        body=body,
                        assistant_id=assistant_id,
                        session_name=session_name,
                        binding=binding,
                        existing_conditions=existing_conditions,
                        desktop_required=desktop_required,
                        source_reason="desktop_liveness_failed",
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
        ready_context = (
            bind_causal_context(
                child_causal_context(
                    caller="controller.reconcile.desktop_ready",
                    parent=ready_signal_parent_context,
                    reason=f"persisted_signal:{SIGNAL_DESKTOP_READY}",
                ),
            )
            if ready_signal_parent_context
            else nullcontext()
        )
        with ready_context:
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

    vm_readiness_deadline_seconds = _vm_readiness_deadline_seconds()
    if _binding_deadline_exceeded(
        binding,
        "guestHandshakeStartedAt",
        vm_readiness_deadline_seconds,
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
                    f"{int(vm_readiness_deadline_seconds)}s"
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
                f"{int(vm_readiness_deadline_seconds)}s"
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
    with bind_causal_context(
        build_causal_context(
            caller="controller.on_session_change",
            reason="watch_event",
        ),
    ):
        _update_status_for_session(body)


@kopf.timer(
    SETTINGS.assistant_session_group,
    SETTINGS.assistant_session_version,
    SETTINGS.assistant_session_plural,
    interval=RECONCILE_INTERVAL_SECONDS,
)
def reconcile_session(body, **_):
    with bind_causal_context(
        build_causal_context(
            caller="controller.reconcile_timer",
            reason="timer",
        ),
    ):
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
    with bind_causal_context(
        build_causal_context(
            caller="controller.session_delete",
            reason="delete_handler",
        ),
    ):
        assert _core_api is not None
        latest_body = _refresh_session_snapshot(body)
        if latest_body is None:
            return

        session_name = str(latest_body.get("metadata", {}).get("name", "") or "")
        spec = latest_body.get("spec", {})
        assistant_id = str(spec.get("assistantId", "") or "")
        activation_id = str(spec.get("activationId", "") or "")
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
                secret = _core_api.read_namespaced_secret(
                    name=secret_name,
                    namespace=WATCH_NAMESPACE,
                )
            except ApiException as e:
                if e.status == 404:
                    secret = None
                else:
                    logger.exception(
                        "Failed reading bootstrap secret for deleted AssistantSession",
                    )
                    raise kopf.TemporaryError(
                        "AssistantSession bootstrap secret cleanup failed",
                        delay=RECONCILE_INTERVAL_SECONDS,
                    ) from e
            if secret is not None and bootstrap_secret_owned_by_session(
                secret,
                assistant_id=assistant_id,
                activation_id=activation_id,
                secret_name=secret_name,
            ):
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
            elif secret is not None:
                annotations = getattr(secret.metadata, "annotations", None) or {}
                emit_observability_event(
                    "controller.session_delete.secret_cleanup_skipped",
                    assistant_id=assistant_id or None,
                    session_name=session_name or None,
                    activation_id=activation_id or None,
                    secret_name=secret_name,
                    secret_owner_session_name=(
                        str(annotations.get(SESSION_REF_ANNOTATION, "") or "") or None
                    ),
                    secret_owner_activation_id=(
                        str(annotations.get(ACTIVATION_ID_ANNOTATION, "") or "") or None
                    ),
                )

        emit_observability_event(
            "controller.session_delete.finalized",
            assistant_id=assistant_id or None,
            session_name=session_name or None,
        )


@kopf.on.probe(id="health")
def health_probe(**_):
    return {"ts": _now_iso(), "namespace": WATCH_NAMESPACE}
