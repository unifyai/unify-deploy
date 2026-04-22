from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
import base64
import json
import logging
import os
import uuid
from typing import Any

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from common.settings import SETTINGS
from .helpers import setup_kubernetes_client
from .observability import causal_log_fields, causal_signal_payload

logger = logging.getLogger(__name__)

SESSION_REF_LABEL = "assistantsession.unify.ai/name"
SESSION_REF_ANNOTATION = "assistantsession.unify.ai/name"
BINDING_ID_LABEL = "assistantsession.unify.ai/binding-id"
BINDING_ID_ANNOTATION = "assistantsession.unify.ai/binding-id"
CONTAINER_READY_ANNOTATION = "assistantsession.unify.ai/container-ready"
ACTIVATION_ID_ANNOTATION = "assistantsession.unify.ai/activation-id"

DESIRED_STATE_RUNNING = "Running"
DESIRED_STATE_STOPPED = "Stopped"
TERMINAL_PHASES = {"Released", "Failed"}
ACTIVE_PHASES = {
    "PendingJob",
    "PendingContainer",
    "PendingVM",
    "PendingGuest",
    "Active",
}
SIGNAL_JOB_BINDING = "jobBinding"
SIGNAL_DESKTOP_READY = "desktopReady"
SIGNAL_VM_GUEST_HEALTH = "vmGuestHealth"
SIGNAL_VM_RELEASE_REQUEST = "vmReleaseRequest"
SIGNAL_VM_RELEASE_COMPLETE = "vmReleaseComplete"
SUSPEND_INTENT_STOP = "stop"
SUSPEND_INTENT_REPLACE = "replace"
SUSPEND_INTENT_UNKNOWN = "unknown"
_STATUS_UNSET = object()
_MAX_CAS_RETRIES = 3
_ASSISTANT_SESSION_SPEC_CONVERGENCE_IGNORED_FIELDS = frozenset({"requestedAt"})
_RELEASED_BINDINGS_HISTORY_LIMIT = 20


class AssistantSessionTerminatingError(RuntimeError):
    """Raised when a caller tries to reuse a deleting AssistantSession."""


def _sanitize_for_k8s(value: str) -> str:
    return str(value).lower().replace("_", "-")


def assistant_session_name(assistant_id: str) -> str:
    return f"assistant-session-{_sanitize_for_k8s(assistant_id)}"


def assistant_session_secret_name(assistant_id: str, activation_id: str) -> str:
    """Return the activation-scoped bootstrap Secret name for a session."""

    sanitized_activation_id = _sanitize_for_k8s(activation_id)
    if not sanitized_activation_id:
        raise ValueError("bootstrap Secret names require a non-empty activation_id")
    return (
        "assistant-session-bootstrap-"
        f"{_sanitize_for_k8s(assistant_id)}-{sanitized_activation_id}"
    )


def _bootstrap_secret_annotations(
    *,
    assistant_id: str,
    activation_id: str,
) -> dict[str, str]:
    """Return the owner annotations recorded on bootstrap secrets."""

    return {
        SESSION_REF_ANNOTATION: assistant_session_name(assistant_id),
        ACTIVATION_ID_ANNOTATION: str(activation_id or ""),
    }


def assistant_session_is_terminating(session: dict[str, Any] | None) -> bool:
    """Return whether Kubernetes has started deleting the session object."""

    metadata = (session or {}).get("metadata") or {}
    return bool(metadata.get("deletionTimestamp"))


def assistant_session_desired_state(session: dict[str, Any] | None) -> str:
    """Return the effective desired runtime state for a session.

    A terminating AssistantSession behaves like ``Stopped`` even if its stored
    spec still says ``Running``. This lets the controller and runtime status
    checks treat Kubernetes deletion as authoritative termination intent while
    the object remains visible behind the finalizer.
    """

    if assistant_session_is_terminating(session):
        return DESIRED_STATE_STOPPED
    spec = (session or {}).get("spec", {})
    desired_state = str(spec.get("desiredState", "") or "")
    return desired_state or DESIRED_STATE_RUNNING


def assistant_session_stop_requested(session: dict[str, Any] | None) -> bool:
    """Return whether a stop is pending on the session's current binding.

    Used by ``/infra/job/start`` as an in-plane fail-safe: Orchestra's
    membership-change runtime barrier is the primary guarantee that a wake
    cannot race a stop, but this predicate closes the reconciliation window
    where ``desiredState`` has been patched to ``Stopped`` and ``suspendIntent``
    records the stop against the current binding, yet the controller has not
    yet advanced the phase into ``Released``.
    """

    current_binding = session_binding(session)
    current_binding_id = binding_id(current_binding)
    if not current_binding_id:
        return False
    if assistant_session_desired_state(session) != DESIRED_STATE_STOPPED:
        return False
    return (
        suspend_intent_value(
            session_suspend_intent(session),
            binding_id=current_binding_id,
        )
        == SUSPEND_INTENT_STOP
    )


def session_desktop_required(session: dict[str, Any] | None) -> bool:
    """Return whether the session currently requires a managed desktop."""

    desktop = ((session or {}).get("spec") or {}).get("desktop") or {}
    return bool(desktop.get("required", False))


def session_desktop_mode(session: dict[str, Any] | None) -> str:
    """Return the managed desktop mode configured for the session."""

    desktop = ((session or {}).get("spec") or {}).get("desktop") or {}
    return str(desktop.get("mode", "") or "")


def session_binding(session: dict[str, Any] | None) -> dict[str, Any]:
    """Return the controller-owned binding object for a session."""

    status = (session or {}).get("status", {})
    binding = status.get("binding")
    return binding if isinstance(binding, dict) else {}


def binding_job_ref(binding: dict[str, Any] | None) -> dict[str, Any]:
    """Return the bound Job reference from a binding."""

    value = (binding or {}).get("jobRef")
    return value if isinstance(value, dict) else {}


def binding_pod_ref(binding: dict[str, Any] | None) -> dict[str, Any]:
    """Return the bound Pod reference from a binding."""

    value = (binding or {}).get("podRef")
    return value if isinstance(value, dict) else {}


def binding_vm_ref(binding: dict[str, Any] | None) -> dict[str, Any]:
    """Return the bound VM reference from a binding."""

    value = (binding or {}).get("vmRef")
    return value if isinstance(value, dict) else {}


def binding_vm_assignment(binding: dict[str, Any] | None) -> dict[str, Any]:
    """Return the persisted VM assignment state for a binding."""

    value = (binding or {}).get("vmAssignment")
    return value if isinstance(value, dict) else {}


def binding_desktop_url(binding: dict[str, Any] | None) -> str:
    """Return the resolved desktop URL stored on the binding."""

    return str((binding or {}).get("desktopUrl", "") or "")


def binding_id(binding: dict[str, Any] | None) -> str:
    """Return the immutable binding identifier."""

    return str((binding or {}).get("id", "") or "")


def binding_release_generation(binding: dict[str, Any] | None) -> int:
    """Return the current release attempt generation for a binding."""

    value = (binding or {}).get("releaseGeneration")
    try:
        generation = int(value)
    except (TypeError, ValueError):
        return 0
    return generation if generation > 0 else 0


def session_suspend_intent(session: dict[str, Any] | None) -> dict[str, Any]:
    """Return the latest persisted suspend intent recorded on session status."""

    status = (session or {}).get("status", {})
    suspend_intent = status.get("suspendIntent")
    return suspend_intent if isinstance(suspend_intent, dict) else {}


def suspend_intent_binding_id(suspend_intent: dict[str, Any] | None) -> str:
    """Return the binding id attached to a persisted suspend intent."""

    return str((suspend_intent or {}).get("bindingId", "") or "")


def suspend_intent_value(
    suspend_intent: dict[str, Any] | None,
    *,
    binding_id: str | None = None,
) -> str:
    """Return the persisted suspend intent for one binding.

    When ``binding_id`` is provided, intents recorded for a different binding are
    ignored. Invalid or missing persisted values normalize to ``unknown`` so the
    controller can fall back to its conservative recovery behavior.
    """

    target_binding_id = str(binding_id or "")
    recorded_binding_id = suspend_intent_binding_id(suspend_intent)
    if (
        target_binding_id
        and recorded_binding_id
        and recorded_binding_id != target_binding_id
    ):
        return ""

    value = str((suspend_intent or {}).get("intent", "") or "").strip().lower()
    if value == SUSPEND_INTENT_STOP:
        return SUSPEND_INTENT_STOP
    if value == SUSPEND_INTENT_REPLACE:
        return SUSPEND_INTENT_REPLACE
    if suspend_intent:
        return SUSPEND_INTENT_UNKNOWN
    return ""


def build_suspend_intent(
    *,
    binding_id: str,
    intent: str,
    source: str | None = None,
    source_reason: str | None = None,
    requested_at: str | None = None,
    job_name: str | None = None,
) -> dict[str, Any]:
    """Build the canonical persisted suspend-intent payload."""

    normalized_intent = (
        suspend_intent_value({"intent": intent}) or SUSPEND_INTENT_UNKNOWN
    )
    payload = {
        "bindingId": binding_id,
        "intent": normalized_intent,
        "source": source,
        "sourceReason": source_reason,
        "requestedAt": requested_at or datetime.now(timezone.utc).isoformat(),
        "jobName": job_name,
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def session_signals(session: dict[str, Any] | None) -> dict[str, Any]:
    """Return the latest binding-scoped signals recorded for a session."""

    status = (session or {}).get("status", {})
    signals = status.get("signals")
    return signals if isinstance(signals, dict) else {}


def session_signal(
    session: dict[str, Any] | None,
    signal_name: str,
) -> dict[str, Any]:
    """Return one binding-scoped signal payload from ``status.signals``."""

    value = session_signals(session).get(signal_name)
    return value if isinstance(value, dict) else {}


def session_released_bindings(session: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the binding-scoped release-completion ledger from status."""

    status = (session or {}).get("status", {})
    released_bindings = status.get("releasedBindings")
    if not isinstance(released_bindings, list):
        return []
    return [entry for entry in released_bindings if isinstance(entry, dict)]


def released_binding(
    session: dict[str, Any] | None,
    target_binding_id: str,
) -> dict[str, Any]:
    """Return one persisted released-binding entry by binding id."""

    target = str(target_binding_id or "")
    if not target:
        return {}
    for entry in session_released_bindings(session):
        if str(entry.get("bindingId", "") or "") == target:
            return entry
    return {}


def build_released_binding(
    *,
    binding_id: str,
    release_completed_at: str | None = None,
    release_requested_at: str | None = None,
) -> dict[str, Any]:
    """Build the canonical released-binding ledger entry."""

    fields = {
        "bindingId": binding_id,
        "releaseRequestedAt": release_requested_at,
        "releaseCompletedAt": release_completed_at
        or datetime.now(timezone.utc).isoformat(),
    }
    return {key: value for key, value in fields.items() if value not in (None, "")}


def _upsert_released_binding_entry(
    released_bindings: list[dict[str, Any]],
    entry: dict[str, Any],
) -> list[dict[str, Any]]:
    """Append or replace one released-binding entry, keeping bounded history."""

    target_binding_id = str(entry.get("bindingId", "") or "")
    if not target_binding_id:
        return deepcopy(released_bindings)
    merged = [
        deepcopy(existing)
        for existing in released_bindings
        if str(existing.get("bindingId", "") or "") != target_binding_id
    ]
    merged.append(deepcopy(entry))
    return merged[-_RELEASED_BINDINGS_HISTORY_LIMIT:]


def build_binding_signal(
    *,
    binding_id: str,
    observed_at: str | None = None,
    state: str | None = None,
    message: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a canonical payload for a binding-scoped runtime signal."""

    payload = {
        "bindingId": binding_id,
        "observedAt": observed_at or datetime.now(timezone.utc).isoformat(),
        "state": state,
        "message": message,
        **fields,
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def build_binding_vm_assignment(
    *,
    state: str,
    attempt_id: str | None = None,
    observed_at: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """Build the canonical VM assignment state stored under ``status.binding``."""

    fields = {
        "state": state,
        "attemptId": attempt_id,
        "observedAt": observed_at or datetime.now(timezone.utc).isoformat(),
        "message": message,
    }
    return {key: value for key, value in fields.items() if value not in (None, "")}


def resolve_current_binding_vm_ref(
    binding: dict[str, Any] | None,
    *,
    owned_runtime_vms: list[dict[str, Any]] | None = None,
    disk_vm_name: str | None = None,
) -> dict[str, Any]:
    """Return the unambiguous VM reference for the current binding.

    Release flows can run before a successful VM assignment has been persisted
    into ``status.binding.vmRef``. Recover the current VM only when the
    binding-scoped evidence points to exactly one owner.
    """

    current_vm_ref = binding_vm_ref(binding)
    current_vm_name = str(current_vm_ref.get("name", "") or "")
    if current_vm_name:
        return current_vm_ref

    candidates = owned_runtime_vms or []
    if len(candidates) != 1:
        return {}

    candidate = candidates[0]
    current_binding_id = binding_id(binding)
    candidate_vm_name = str(candidate.get("vm_name", "") or "")
    candidate_binding_id = str(candidate.get("binding_id", "") or "")
    if not candidate_vm_name:
        return {}
    if (
        current_binding_id
        and candidate_binding_id
        and candidate_binding_id != current_binding_id
    ):
        return {}
    if disk_vm_name not in (None, candidate_vm_name):
        return {}

    resolved_vm_ref: dict[str, Any] = {"name": candidate_vm_name}
    candidate_hostname = str(candidate.get("hostname", "") or "")
    candidate_vm_type = str(candidate.get("vm_type", "") or "")
    if candidate_hostname:
        resolved_vm_ref["hostname"] = candidate_hostname
    if candidate_vm_type:
        resolved_vm_ref["vmType"] = candidate_vm_type
    return resolved_vm_ref


def build_binding(
    *,
    binding_id: str,
    job_ref: dict[str, Any] | None = None,
    pod_ref: dict[str, Any] | None = None,
    vm_assignment: dict[str, Any] | None = None,
    vm_ref: dict[str, Any] | None = None,
    desktop_url: str | None = None,
    created_at: str | None = None,
    container_bootstrap_started_at: str | None = None,
    container_ready_at: str | None = None,
    vm_assigned_at: str | None = None,
    guest_handshake_started_at: str | None = None,
    vm_ready_observed_at: str | None = None,
    vm_ready_hostname: str | None = None,
    vm_ready_message_id: str | None = None,
    release_requested_at: str | None = None,
    release_completed_at: str | None = None,
    release_generation: int | None = None,
) -> dict[str, Any]:
    """Build the canonical binding payload stored under ``status.binding``."""

    fields = {
        "id": binding_id,
        "jobRef": job_ref,
        "podRef": pod_ref,
        "vmAssignment": vm_assignment,
        "vmRef": vm_ref,
        "desktopUrl": desktop_url,
        "createdAt": created_at,
        "containerBootstrapStartedAt": container_bootstrap_started_at,
        "containerReadyAt": container_ready_at,
        "vmAssignedAt": vm_assigned_at,
        "guestHandshakeStartedAt": guest_handshake_started_at,
        "vmReadyObservedAt": vm_ready_observed_at,
        "vmReadyHostname": vm_ready_hostname,
        "vmReadyMessageId": vm_ready_message_id,
        "releaseRequestedAt": release_requested_at,
        "releaseCompletedAt": release_completed_at,
        "releaseGeneration": release_generation,
    }
    return {key: value for key, value in fields.items() if value not in (None, "")}


def _binding_from_current(
    current_binding: dict[str, Any] | None,
    *,
    binding_id_override: str | None = None,
    job_ref: dict[str, Any] | None | object = _STATUS_UNSET,
    pod_ref: dict[str, Any] | None | object = _STATUS_UNSET,
    vm_assignment: dict[str, Any] | None | object = _STATUS_UNSET,
    vm_ref: dict[str, Any] | None | object = _STATUS_UNSET,
    desktop_url: str | None | object = _STATUS_UNSET,
    created_at: str | None | object = _STATUS_UNSET,
    container_bootstrap_started_at: str | None | object = _STATUS_UNSET,
    container_ready_at: str | None | object = _STATUS_UNSET,
    vm_assigned_at: str | None | object = _STATUS_UNSET,
    guest_handshake_started_at: str | None | object = _STATUS_UNSET,
    vm_ready_observed_at: str | None | object = _STATUS_UNSET,
    vm_ready_hostname: str | None | object = _STATUS_UNSET,
    vm_ready_message_id: str | None | object = _STATUS_UNSET,
    release_requested_at: str | None | object = _STATUS_UNSET,
    release_completed_at: str | None | object = _STATUS_UNSET,
    release_generation: int | None | object = _STATUS_UNSET,
) -> dict[str, Any]:
    """Return a canonical binding payload using the current binding as a base."""

    current_binding = current_binding or {}
    return build_binding(
        binding_id=binding_id_override or binding_id(current_binding),
        job_ref=(
            binding_job_ref(current_binding) or None
            if job_ref is _STATUS_UNSET
            else job_ref
        ),
        pod_ref=(
            binding_pod_ref(current_binding) or None
            if pod_ref is _STATUS_UNSET
            else pod_ref
        ),
        vm_assignment=(
            binding_vm_assignment(current_binding) or None
            if vm_assignment is _STATUS_UNSET
            else vm_assignment
        ),
        vm_ref=(
            binding_vm_ref(current_binding) or None
            if vm_ref is _STATUS_UNSET
            else vm_ref
        ),
        desktop_url=(
            binding_desktop_url(current_binding) or None
            if desktop_url is _STATUS_UNSET
            else desktop_url
        ),
        created_at=(
            str(current_binding.get("createdAt", "") or "") or None
            if created_at is _STATUS_UNSET
            else created_at
        ),
        container_bootstrap_started_at=(
            str(current_binding.get("containerBootstrapStartedAt", "") or "") or None
            if container_bootstrap_started_at is _STATUS_UNSET
            else container_bootstrap_started_at
        ),
        container_ready_at=(
            str(current_binding.get("containerReadyAt", "") or "") or None
            if container_ready_at is _STATUS_UNSET
            else container_ready_at
        ),
        vm_assigned_at=(
            str(current_binding.get("vmAssignedAt", "") or "") or None
            if vm_assigned_at is _STATUS_UNSET
            else vm_assigned_at
        ),
        guest_handshake_started_at=(
            str(current_binding.get("guestHandshakeStartedAt", "") or "") or None
            if guest_handshake_started_at is _STATUS_UNSET
            else guest_handshake_started_at
        ),
        vm_ready_observed_at=(
            str(current_binding.get("vmReadyObservedAt", "") or "") or None
            if vm_ready_observed_at is _STATUS_UNSET
            else vm_ready_observed_at
        ),
        vm_ready_hostname=(
            str(current_binding.get("vmReadyHostname", "") or "") or None
            if vm_ready_hostname is _STATUS_UNSET
            else vm_ready_hostname
        ),
        vm_ready_message_id=(
            str(current_binding.get("vmReadyMessageId", "") or "") or None
            if vm_ready_message_id is _STATUS_UNSET
            else vm_ready_message_id
        ),
        release_requested_at=(
            str(current_binding.get("releaseRequestedAt", "") or "") or None
            if release_requested_at is _STATUS_UNSET
            else release_requested_at
        ),
        release_completed_at=(
            str(current_binding.get("releaseCompletedAt", "") or "") or None
            if release_completed_at is _STATUS_UNSET
            else release_completed_at
        ),
        release_generation=(
            binding_release_generation(current_binding) or None
            if release_generation is _STATUS_UNSET
            else release_generation
        ),
    )


def _binding_release_field_regressions(
    current_binding: dict[str, Any] | None,
    next_binding: dict[str, Any] | None,
) -> dict[str, dict[str, str | int | None]]:
    """Return release lifecycle fields that would be cleared on the same binding."""

    current_binding_id = binding_id(current_binding)
    next_binding_id = binding_id(next_binding)
    if not current_binding_id or current_binding_id != next_binding_id:
        return {}

    regressions: dict[str, dict[str, str | int | None]] = {}
    for field_name in ("releaseRequestedAt", "releaseCompletedAt", "releaseGeneration"):
        current_value = (current_binding or {}).get(field_name)
        next_value = (next_binding or {}).get(field_name)
        if field_name == "releaseGeneration":
            current_value = binding_release_generation(current_binding) or None
            next_value = binding_release_generation(next_binding) or None
        else:
            current_value = str(current_value or "") or None
            next_value = str(next_value or "") or None
        if current_value and not next_value:
            regressions[field_name] = {
                "before": current_value,
                "after": None,
            }
    return regressions


def _compact_observability_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in fields.items()
        if value not in (None, "", [], {}, ())
    }


def _condition_states(conditions: list[dict[str, Any]] | None) -> dict[str, str]:
    result = {}
    for condition in conditions or []:
        condition_type = str(condition.get("type", "") or "")
        if not condition_type:
            continue
        status = str(condition.get("status", "") or "")
        reason = str(condition.get("reason", "") or "")
        result[condition_type] = f"{status}:{reason}" if reason else status
    return result


def assistant_session_observability_fields(
    session: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    metadata = session.get("metadata", {}) if session else {}
    spec = session.get("spec", {}) if session else {}
    status = session.get("status", {}) if session else {}
    binding = session_binding(session)
    assignment = binding_vm_assignment(binding)
    vm_ref = binding_vm_ref(binding)
    suspend_intent = session_suspend_intent(session)

    fields = {
        "assistant_id": overrides.pop("assistant_id", spec.get("assistantId")),
        "session_name": overrides.pop("session_name", metadata.get("name")),
        "activation_id": overrides.pop("activation_id", spec.get("activationId")),
        "desired_state": overrides.pop(
            "desired_state",
            assistant_session_desired_state(session) if session else None,
        ),
        "observed_activation_id": overrides.pop(
            "observed_activation_id",
            status.get("observedActivationId"),
        ),
        "phase": overrides.pop("phase", status.get("phase")),
        "binding_id": overrides.pop("binding_id", binding_id(binding)),
        "vm_assignment_state": overrides.pop(
            "vm_assignment_state",
            assignment.get("state"),
        ),
        "vm_assignment_attempt_id": overrides.pop(
            "vm_assignment_attempt_id",
            assignment.get("attemptId"),
        ),
        "vm_assignment_message": overrides.pop(
            "vm_assignment_message",
            assignment.get("message"),
        ),
        "job_name": overrides.pop("job_name", binding_job_ref(binding).get("name")),
        "pod_name": overrides.pop("pod_name", binding_pod_ref(binding).get("name")),
        "vm_name": overrides.pop("vm_name", vm_ref.get("name")),
        "vm_hostname": overrides.pop(
            "vm_hostname",
            vm_ref.get("hostname"),
        ),
        "release_generation": overrides.pop(
            "release_generation",
            binding_release_generation(binding),
        ),
        "desktop_url": overrides.pop("desktop_url", binding_desktop_url(binding)),
        "last_error": overrides.pop("last_error", status.get("lastError")),
        "suspend_intent": overrides.pop(
            "suspend_intent",
            suspend_intent_value(suspend_intent),
        ),
        "suspend_intent_binding_id": overrides.pop(
            "suspend_intent_binding_id",
            suspend_intent_binding_id(suspend_intent),
        ),
        "suspend_intent_source": overrides.pop(
            "suspend_intent_source",
            suspend_intent.get("source"),
        ),
        "condition_states": overrides.pop(
            "condition_states",
            _condition_states(status.get("conditions")),
        ),
    }
    fields.update(overrides)
    return _compact_observability_fields(fields)


def emit_observability_event(event: str, **fields: Any) -> None:
    logger.info(
        "OBS_EVENT %s",
        json.dumps(
            {
                "event": event,
                **causal_log_fields(),
                **_compact_observability_fields(fields),
            },
            sort_keys=True,
            default=str,
        ),
    )


def _get_storage_client():
    from google.cloud import storage
    from google.oauth2.service_account import Credentials

    creds_json = os.getenv("GCP_SA_KEY")
    if creds_json:
        creds = Credentials.from_service_account_info(json.loads(creds_json))
        return storage.Client(credentials=creds)
    return storage.Client()


def get_latest_unity_image() -> str:
    storage_client = _get_storage_client()
    bucket = storage_client.bucket("unity-image-hash")
    blob = bucket.blob(SETTINGS.image_hash_blob)
    commit_hash = blob.download_as_text().strip()
    return f"{SETTINGS.image_registry}/{SETTINGS.unity_image_name}:{commit_hash}"


def get_custom_objects_api() -> k8s_client.CustomObjectsApi | None:
    batch_api, _, _, _ = setup_kubernetes_client()
    if not batch_api:
        return None
    return k8s_client.CustomObjectsApi(batch_api.api_client)


def get_assistant_session(
    custom_api: k8s_client.CustomObjectsApi,
    namespace: str,
    assistant_id: str,
) -> dict[str, Any] | None:
    name = assistant_session_name(assistant_id)
    try:
        return custom_api.get_namespaced_custom_object(
            group=SETTINGS.assistant_session_group,
            version=SETTINGS.assistant_session_version,
            namespace=namespace,
            plural=SETTINGS.assistant_session_plural,
            name=name,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _read_secret_or_none(core_api, namespace: str, secret_name: str):
    try:
        return core_api.read_namespaced_secret(
            name=secret_name,
            namespace=namespace,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def _secret_startup_payload(secret) -> dict[str, Any]:
    data = getattr(secret, "data", None) or {}
    raw = data.get("startup.json", "")
    if raw:
        return json.loads(base64.b64decode(raw).decode("utf-8"))

    string_data = getattr(secret, "string_data", None) or {}
    raw_string = string_data.get("startup.json", "")
    if raw_string:
        return json.loads(raw_string)
    return {}


def _secret_matches_bootstrap_request(
    secret,
    *,
    assistant_id: str,
    activation_id: str,
    payload: dict[str, Any],
) -> bool:
    """Return whether a bootstrap secret matches both payload and owner."""

    try:
        metadata = getattr(secret, "metadata", None)
        annotations = getattr(metadata, "annotations", None) or {}
        expected_annotations = _bootstrap_secret_annotations(
            assistant_id=assistant_id,
            activation_id=activation_id,
        )
        return _secret_startup_payload(secret) == payload and all(
            str(annotations.get(key, "") or "") == value
            for key, value in expected_annotations.items()
        )
    except Exception:
        return False


def bootstrap_secret_owned_by_session(
    secret,
    *,
    assistant_id: str,
    activation_id: str,
    secret_name: str,
) -> bool:
    """Return whether a Secret still belongs to the terminating session.

    Legacy secrets may predate owner annotations. For those, matching the
    referenced Secret name is the strongest ownership signal available.
    """

    metadata = getattr(secret, "metadata", None)
    if metadata is None:
        return False

    actual_name = str(getattr(metadata, "name", "") or secret_name)
    if actual_name != secret_name:
        return False

    annotations = getattr(metadata, "annotations", None) or {}
    owner_session_name = str(annotations.get(SESSION_REF_ANNOTATION, "") or "")
    owner_activation_id = str(annotations.get(ACTIVATION_ID_ANNOTATION, "") or "")
    if owner_session_name and owner_session_name != assistant_session_name(
        assistant_id,
    ):
        return False
    if owner_activation_id and owner_activation_id != str(activation_id or ""):
        return False
    return True


def delete_bootstrap_secret_if_owned(
    core_api,
    namespace: str,
    *,
    assistant_id: str,
    activation_id: str,
    secret_name: str,
) -> bool:
    """Delete a bootstrap Secret only when it still belongs to that activation."""

    secret = _read_secret_or_none(core_api, namespace, secret_name)
    if secret is None:
        return False
    if not bootstrap_secret_owned_by_session(
        secret,
        assistant_id=assistant_id,
        activation_id=activation_id,
        secret_name=secret_name,
    ):
        annotations = (
            getattr(getattr(secret, "metadata", None), "annotations", None) or {}
        )
        emit_observability_event(
            "assistantsession.bootstrap_secret_delete_skipped",
            assistant_id=assistant_id,
            activation_id=activation_id,
            secret_name=secret_name,
            secret_owner_session_name=(
                str(annotations.get(SESSION_REF_ANNOTATION, "") or "") or None
            ),
            secret_owner_activation_id=(
                str(annotations.get(ACTIVATION_ID_ANNOTATION, "") or "") or None
            ),
        )
        return False
    try:
        core_api.delete_namespaced_secret(name=secret_name, namespace=namespace)
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    emit_observability_event(
        "assistantsession.bootstrap_secret_deleted",
        assistant_id=assistant_id,
        activation_id=activation_id,
        secret_name=secret_name,
    )
    return True


def delete_assistant_session(
    custom_api: k8s_client.CustomObjectsApi,
    namespace: str,
    assistant_id: str,
) -> bool:
    name = assistant_session_name(assistant_id)
    try:
        custom_api.delete_namespaced_custom_object(
            group=SETTINGS.assistant_session_group,
            version=SETTINGS.assistant_session_version,
            namespace=namespace,
            plural=SETTINGS.assistant_session_plural,
            name=name,
        )
        emit_observability_event(
            "assistantsession.delete_requested",
            assistant_id=assistant_id,
            session_name=name,
        )
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise


def create_or_update_bootstrap_secret(
    core_api,
    namespace: str,
    assistant_id: str,
    activation_id: str,
    payload: dict[str, Any],
) -> str:
    secret_name = assistant_session_secret_name(assistant_id, activation_id)
    body = k8s_client.V1Secret(
        metadata=k8s_client.V1ObjectMeta(
            name=secret_name,
            namespace=namespace,
            annotations=_bootstrap_secret_annotations(
                assistant_id=assistant_id,
                activation_id=activation_id,
            ),
        ),
        type="Opaque",
        string_data={"startup.json": json.dumps(payload)},
    )
    existing_secret = _read_secret_or_none(core_api, namespace, secret_name)
    if existing_secret is None:
        try:
            core_api.create_namespaced_secret(namespace=namespace, body=body)
            emit_observability_event(
                "assistantsession.bootstrap_secret_created",
                assistant_id=assistant_id,
                activation_id=activation_id,
                secret_name=secret_name,
            )
            return secret_name
        except ApiException as e:
            if e.status != 409:
                raise
            emit_observability_event(
                "assistantsession.bootstrap_secret_create_conflict",
                assistant_id=assistant_id,
                activation_id=activation_id,
                secret_name=secret_name,
                error=str(e),
            )
            existing_secret = _read_secret_or_none(core_api, namespace, secret_name)
            if existing_secret is None:
                raise RuntimeError(
                    f"Bootstrap secret {secret_name} still missing after create conflict",
                )

    for _attempt in range(3):
        if _secret_matches_bootstrap_request(
            existing_secret,
            assistant_id=assistant_id,
            activation_id=activation_id,
            payload=payload,
        ):
            emit_observability_event(
                "assistantsession.bootstrap_secret_already_current",
                assistant_id=assistant_id,
                activation_id=activation_id,
                secret_name=secret_name,
            )
            return secret_name
        body.metadata.resource_version = existing_secret.metadata.resource_version
        try:
            core_api.replace_namespaced_secret(
                name=secret_name,
                namespace=namespace,
                body=body,
            )
            emit_observability_event(
                "assistantsession.bootstrap_secret_replaced",
                assistant_id=assistant_id,
                activation_id=activation_id,
                secret_name=secret_name,
                resource_version=body.metadata.resource_version,
            )
            return secret_name
        except ApiException as e:
            if e.status != 409:
                raise
            emit_observability_event(
                "assistantsession.bootstrap_secret_replace_conflict",
                assistant_id=assistant_id,
                secret_name=secret_name,
                attempt=_attempt + 1,
            )
            existing_secret = _read_secret_or_none(core_api, namespace, secret_name)
            if existing_secret is None:
                raise RuntimeError(
                    f"Bootstrap secret {secret_name} disappeared during replace retry",
                )
    final_secret = _read_secret_or_none(core_api, namespace, secret_name)
    if final_secret is not None and _secret_matches_bootstrap_request(
        final_secret,
        assistant_id=assistant_id,
        activation_id=activation_id,
        payload=payload,
    ):
        emit_observability_event(
            "assistantsession.bootstrap_secret_converged_after_conflicts",
            assistant_id=assistant_id,
            activation_id=activation_id,
            secret_name=secret_name,
        )
        return secret_name
    raise RuntimeError(
        f"Bootstrap secret {secret_name} did not converge to the requested payload",
    )


def read_bootstrap_secret(
    core_api,
    namespace: str,
    secret_name: str,
) -> dict[str, Any]:
    secret = core_api.read_namespaced_secret(name=secret_name, namespace=namespace)
    return _secret_startup_payload(secret)


def build_assistant_session_spec(
    *,
    assistant_id: str,
    user_id: str,
    medium: str,
    desktop_mode: str,
    startup_secret_ref: str,
    activation_id: str,
    desired_state: str = DESIRED_STATE_RUNNING,
) -> dict[str, Any]:
    desktop_required = desktop_mode in ("windows", "ubuntu")
    return {
        "assistantId": str(assistant_id),
        "userId": str(user_id),
        "medium": medium,
        "desiredState": desired_state,
        "desktop": {
            "required": desktop_required,
            "mode": desktop_mode,
        },
        "startupSecretRef": startup_secret_ref,
        "protocolVersion": SETTINGS.assistant_session_protocol_version,
        "activationId": activation_id,
        "requestedAt": datetime.now(timezone.utc).isoformat(),
    }


def _assistant_session_spec_matches(
    session: dict[str, Any] | None,
    desired_spec: dict[str, Any],
) -> bool:
    current_spec = (session or {}).get("spec", {})
    compare_keys = (
        set(desired_spec.keys()) - _ASSISTANT_SESSION_SPEC_CONVERGENCE_IGNORED_FIELDS
    )
    return all(current_spec.get(key) == desired_spec.get(key) for key in compare_keys)


def _raise_if_session_terminating(
    session: dict[str, Any] | None,
    *,
    assistant_id: str,
    session_name: str,
    activation_id: str | None,
) -> None:
    """Reject attempts to reuse an AssistantSession that is deleting."""

    if not assistant_session_is_terminating(session):
        return
    emit_observability_event(
        "assistantsession.update_blocked_terminating",
        assistant_id=assistant_id,
        session_name=session_name,
        activation_id=activation_id,
    )
    raise AssistantSessionTerminatingError(
        f"AssistantSession {session_name} is deleting; retry after cleanup completes",
    )


def create_or_update_assistant_session(
    custom_api: k8s_client.CustomObjectsApi,
    namespace: str,
    assistant_id: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    name = assistant_session_name(assistant_id)
    existing = get_assistant_session(custom_api, namespace, assistant_id)

    metadata = {"name": name, "namespace": namespace}
    body = {
        "apiVersion": (
            f"{SETTINGS.assistant_session_group}/{SETTINGS.assistant_session_version}"
        ),
        "kind": SETTINGS.assistant_session_kind,
        "metadata": metadata,
        "spec": spec,
    }
    last_conflict: ApiException | None = None

    for _attempt in range(_MAX_CAS_RETRIES):
        body["spec"] = spec
        _raise_if_session_terminating(
            existing,
            assistant_id=assistant_id,
            session_name=name,
            activation_id=str(spec.get("activationId", "") or ""),
        )

        if _assistant_session_spec_matches(existing, spec):
            return existing

        if existing is None:
            try:
                return custom_api.create_namespaced_custom_object(
                    group=SETTINGS.assistant_session_group,
                    version=SETTINGS.assistant_session_version,
                    namespace=namespace,
                    plural=SETTINGS.assistant_session_plural,
                    body=body,
                )
            except ApiException as e:
                if e.status != 409:
                    raise
                last_conflict = e
                emit_observability_event(
                    "assistantsession.create_conflict",
                    assistant_id=assistant_id,
                    session_name=name,
                    activation_id=spec.get("activationId"),
                    error=str(e),
                )
                existing = get_assistant_session(custom_api, namespace, assistant_id)
                if existing is None:
                    continue
                _raise_if_session_terminating(
                    existing,
                    assistant_id=assistant_id,
                    session_name=name,
                    activation_id=str(spec.get("activationId", "") or ""),
                )
                if _assistant_session_spec_matches(existing, spec):
                    return existing
                raise e

        rv = existing.get("metadata", {}).get("resourceVersion")
        patch: dict[str, Any] = {"spec": spec}
        if rv:
            patch["metadata"] = {"resourceVersion": rv}
        try:
            existing = custom_api.patch_namespaced_custom_object(
                group=SETTINGS.assistant_session_group,
                version=SETTINGS.assistant_session_version,
                namespace=namespace,
                plural=SETTINGS.assistant_session_plural,
                name=name,
                body=patch,
            )
            if _assistant_session_spec_matches(existing, spec):
                return existing
        except ApiException as e:
            if e.status != 409:
                raise
            last_conflict = e
            emit_observability_event(
                "assistantsession.spec_update_conflict",
                assistant_id=assistant_id,
                session_name=name,
                attempt=_attempt + 1,
            )
            existing = get_assistant_session(custom_api, namespace, assistant_id)
            if existing is None:
                raise
            _raise_if_session_terminating(
                existing,
                assistant_id=assistant_id,
                session_name=name,
                activation_id=str(spec.get("activationId", "") or ""),
            )
    if _assistant_session_spec_matches(
        existing,
        spec,
    ):
        return existing
    if last_conflict is not None:
        raise last_conflict
    raise RuntimeError(
        f"AssistantSession {name} did not converge to the requested spec",
    )


def patch_assistant_session_spec(
    custom_api: k8s_client.CustomObjectsApi,
    namespace: str,
    assistant_id: str,
    *,
    desired_state: str | None = None,
) -> dict[str, Any]:
    """Patch controller-consumed AssistantSession spec fields."""

    name = assistant_session_name(assistant_id)
    spec_patch: dict[str, Any] = {}
    if desired_state is not None:
        spec_patch["desiredState"] = desired_state
    if not spec_patch:
        raise ValueError("patch_assistant_session_spec requires at least one field")

    return custom_api.patch_namespaced_custom_object(
        group=SETTINGS.assistant_session_group,
        version=SETTINGS.assistant_session_version,
        namespace=namespace,
        plural=SETTINGS.assistant_session_plural,
        name=name,
        body={"spec": spec_patch},
    )


def build_condition(
    condition_type: str,
    status: bool,
    reason: str,
    message: str = "",
) -> dict[str, Any]:
    return {
        "type": condition_type,
        "status": "True" if status else "False",
        "reason": reason,
        "message": message,
        "lastTransitionTime": datetime.now(timezone.utc).isoformat(),
    }


def normalize_vm_hostname(value: Any) -> str:
    hostname = str(value or "").strip()
    if hostname.startswith("https://"):
        hostname = hostname.removeprefix("https://")
    elif hostname.startswith("http://"):
        hostname = hostname.removeprefix("http://")
    return hostname.rstrip("/")


def vm_refs_match(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> bool:
    if not left or not right:
        return False

    left_name = str(left.get("name", "") or "").strip()
    right_name = str(right.get("name", "") or "").strip()
    if left_name and right_name and left_name != right_name:
        return False

    left_hostname = normalize_vm_hostname(left.get("hostname"))
    right_hostname = normalize_vm_hostname(right.get("hostname"))
    if left_hostname and right_hostname and left_hostname != right_hostname:
        return False

    return bool((left_name and right_name) or (left_hostname and right_hostname))


def desktop_url_matches_vm_ref(
    desktop_url: str | None,
    vm_ref: dict[str, Any] | None,
) -> bool:
    if not desktop_url or not vm_ref:
        return False
    return vm_refs_match({"hostname": desktop_url}, vm_ref)


def merge_conditions(
    existing: list[dict[str, Any]] | None,
    *updates: dict[str, Any],
) -> list[dict[str, Any]]:
    conditions = {c["type"]: c for c in (existing or []) if c.get("type")}
    for update in updates:
        current = conditions.get(update["type"])
        if current and all(
            current.get(key) == update.get(key)
            for key in ("type", "status", "reason", "message")
        ):
            continue
        conditions[update["type"]] = update
    return [conditions[key] for key in sorted(conditions)]


def patch_assistant_session_status(
    custom_api: k8s_client.CustomObjectsApi,
    namespace: str,
    assistant_id: str,
    *,
    phase: str | None = None,
    observed_activation_id: str | None = None,
    binding: dict[str, Any] | None | object = _STATUS_UNSET,
    last_error: str | None | object = _STATUS_UNSET,
    conditions: list[dict[str, Any]] | None | object = _STATUS_UNSET,
    source: str | None = None,
    bootstrap_retries: int | None | object = _STATUS_UNSET,
    vm_retries: int | None | object = _STATUS_UNSET,
    desktop_probe_failures: int | None | object = _STATUS_UNSET,
    suspend_intent: dict[str, Any] | None | object = _STATUS_UNSET,
    signals: dict[str, Any] | None | object = _STATUS_UNSET,
    released_bindings: list[dict[str, Any]] | object = _STATUS_UNSET,
    expected_binding_id: str | object = _STATUS_UNSET,
    require_desired_state: str | object = _STATUS_UNSET,
    binding_mutator: (
        Callable[
            [dict[str, Any], dict[str, Any]],
            dict[str, Any] | None | object,
        ]
        | None
    ) = None,
    released_bindings_mutator: (
        Callable[
            [dict[str, Any], list[dict[str, Any]]],
            list[dict[str, Any]] | object,
        ]
        | None
    ) = None,
) -> dict[str, Any]:
    """Replace the persisted AssistantSession status with the next canonical state.

    Custom resource status patches use JSON merge semantics, which recursively
    merge nested objects. That is unsafe for ``status.binding`` because a fresh
    binding generation must atomically replace every runtime reference from the
    previous generation. This helper therefore reads the current object,
    materializes the full next ``status`` payload, and replaces the status
    subresource with optimistic concurrency via ``resourceVersion``.
    """

    name = assistant_session_name(assistant_id)
    current: dict[str, Any] = {}
    for _attempt in range(_MAX_CAS_RETRIES):
        current = get_assistant_session(custom_api, namespace, assistant_id) or {}
        current_status = deepcopy(current.get("status") or {})
        next_status = deepcopy(current_status)
        current_binding = session_binding(current)
        current_binding_id = binding_id(current_binding)
        if (
            expected_binding_id is not _STATUS_UNSET
            and current_binding_id != expected_binding_id
        ):
            return current
        if (
            require_desired_state is not _STATUS_UNSET
            and assistant_session_desired_state(current) != require_desired_state
        ):
            return current
        if phase is not None:
            next_status["phase"] = phase
        if observed_activation_id is not None:
            next_status["observedActivationId"] = observed_activation_id
        if binding_mutator is not None:
            next_binding = binding_mutator(current, deepcopy(current_binding))
            if next_binding is _STATUS_UNSET:
                return current
            next_status["binding"] = deepcopy(next_binding)
        elif binding is not _STATUS_UNSET:
            next_status["binding"] = deepcopy(binding)
        if last_error is not _STATUS_UNSET:
            next_status["lastError"] = last_error
        if conditions is not _STATUS_UNSET:
            next_status["conditions"] = deepcopy(conditions)
        if bootstrap_retries is not _STATUS_UNSET:
            next_status["bootstrapRetries"] = bootstrap_retries
        if vm_retries is not _STATUS_UNSET:
            next_status["vmRetries"] = vm_retries
        if desktop_probe_failures is not _STATUS_UNSET:
            next_status["desktopProbeFailures"] = desktop_probe_failures
        if suspend_intent is not _STATUS_UNSET:
            if suspend_intent is None:
                next_status.pop("suspendIntent", None)
            else:
                next_status["suspendIntent"] = deepcopy(suspend_intent)
        if signals is not _STATUS_UNSET:
            next_status["signals"] = deepcopy(signals)
        if released_bindings_mutator is not None:
            next_released_bindings = released_bindings_mutator(
                current,
                deepcopy(session_released_bindings(current)),
            )
            if next_released_bindings is _STATUS_UNSET:
                return current
            next_status["releasedBindings"] = deepcopy(next_released_bindings)
        elif released_bindings is not _STATUS_UNSET:
            next_status["releasedBindings"] = deepcopy(released_bindings)

        next_binding = session_binding({"status": next_status})
        next_binding_id = binding_id(next_binding)
        if (
            (binding is not _STATUS_UNSET or binding_mutator is not None)
            and current_binding_id != next_binding_id
            and signals is _STATUS_UNSET
        ):
            next_status["signals"] = {}
        release_field_regressions = (
            _binding_release_field_regressions(current_binding, next_binding)
            if (binding is not _STATUS_UNSET or binding_mutator is not None)
            else {}
        )
        if release_field_regressions:
            current_binding_name = binding_id(current_binding) or None
            emit_observability_event(
                "assistantsession.binding_release_fields_cleared",
                assistant_id=assistant_id,
                session_name=name,
                source=source,
                binding_id=current_binding_name,
                current_phase=current_status.get("phase"),
                next_phase=next_status.get("phase"),
                current_release_requested_at=current_binding.get("releaseRequestedAt"),
                next_release_requested_at=next_binding.get("releaseRequestedAt"),
                current_release_completed_at=current_binding.get("releaseCompletedAt"),
                next_release_completed_at=next_binding.get("releaseCompletedAt"),
                cleared_fields=release_field_regressions,
            )

        if current_status == next_status:
            return current

        rv = current.get("metadata", {}).get("resourceVersion")
        body = deepcopy(current) if current else {"metadata": {"name": name}}
        body["status"] = next_status
        metadata = body.setdefault("metadata", {})
        metadata["name"] = name
        if rv:
            metadata["resourceVersion"] = rv
        try:
            result = custom_api.replace_namespaced_custom_object_status(
                group=SETTINGS.assistant_session_group,
                version=SETTINGS.assistant_session_version,
                namespace=namespace,
                plural=SETTINGS.assistant_session_plural,
                name=name,
                body=body,
            )
        except ApiException as e:
            if e.status != 409 or _attempt >= _MAX_CAS_RETRIES - 1:
                raise
            emit_observability_event(
                "assistantsession.status_patch_conflict",
                assistant_id=assistant_id,
                session_name=name,
                source=source,
                attempt=_attempt + 1,
            )
            continue

        previous_fields = assistant_session_observability_fields(
            current,
            source=source,
        )
        current_fields = assistant_session_observability_fields(
            result,
            source=source,
        )
        changed_fields = {
            key: {
                "before": previous_fields.get(key),
                "after": current_fields.get(key),
            }
            for key in sorted(set(previous_fields) | set(current_fields))
            if previous_fields.get(key) != current_fields.get(key)
        }
        if changed_fields:
            emit_observability_event(
                "assistantsession.status_patch",
                **current_fields,
                changed_fields=changed_fields,
            )
        return result

    return current


def record_released_binding(
    custom_api: k8s_client.CustomObjectsApi,
    namespace: str,
    assistant_id: str,
    *,
    binding_id: str,
    release_completed_at: str,
    release_requested_at: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Persist durable release completion for one binding generation.

    Assistant-level phase can move on to a fresh binding immediately after
    teardown finishes. This ledger preserves the completion fact so callers
    can wait on the old binding by id even after the session has advanced.
    """

    entry = build_released_binding(
        binding_id=binding_id,
        release_completed_at=release_completed_at,
        release_requested_at=release_requested_at,
    )
    updated = patch_assistant_session_status(
        custom_api,
        namespace,
        assistant_id,
        source=source,
        released_bindings_mutator=lambda _current, current_released_bindings: (
            _upsert_released_binding_entry(current_released_bindings, entry)
        ),
    )
    emit_observability_event(
        "assistantsession.released_binding_persisted",
        assistant_id=assistant_id,
        session_name=assistant_session_name(assistant_id),
        binding_id=binding_id,
        release_requested_at=release_requested_at,
        release_completed_at=release_completed_at,
        source=source,
    )
    return updated


def record_assistant_session_signal(
    custom_api: k8s_client.CustomObjectsApi,
    namespace: str,
    assistant_id: str,
    *,
    signal_name: str,
    payload: dict[str, Any] | None,
    source: str | None = None,
) -> dict[str, Any]:
    """Persist or clear a binding-scoped signal without changing controller state."""

    current = get_assistant_session(custom_api, namespace, assistant_id) or {}
    signals = deepcopy(session_signals(current))
    stored_payload = deepcopy(payload) if payload is not None else None
    if stored_payload is not None:
        if source and "source" not in stored_payload:
            stored_payload["source"] = source
        signal_causal = causal_signal_payload()
        if signal_causal and "causal" not in stored_payload:
            stored_payload["causal"] = signal_causal
    if payload is None:
        signals.pop(signal_name, None)
    else:
        signals[signal_name] = stored_payload
    updated = patch_assistant_session_status(
        custom_api,
        namespace,
        assistant_id,
        signals=signals,
        source=source,
    )
    signal_binding_id = None
    signal_state = None
    signal_observed_at = None
    signal_vm_name = None
    signal_vm_hostname = None
    signal_caller = None
    signal_release_generation = None
    if stored_payload is not None:
        signal_binding_id = stored_payload.get("bindingId")
        signal_state = stored_payload.get("state")
        signal_observed_at = stored_payload.get("observedAt")
        try:
            signal_release_generation = int(stored_payload.get("releaseGeneration"))
        except (TypeError, ValueError):
            signal_release_generation = None
        signal_vm_name = (
            str(((stored_payload.get("vmRef") or {}).get("name")) or "")
            or str(stored_payload.get("vmName") or "")
            or None
        )
        signal_vm_hostname = (
            str(((stored_payload.get("vmRef") or {}).get("hostname")) or "")
            or str(stored_payload.get("hostname") or "")
            or None
        )
        signal_caller = (
            str(((stored_payload.get("causal") or {}).get("caller")) or "") or None
        )
    emit_observability_event(
        (
            "assistantsession.signal_persisted"
            if stored_payload is not None
            else "assistantsession.signal_cleared"
        ),
        assistant_id=assistant_id,
        session_name=assistant_session_name(assistant_id),
        signal_name=signal_name,
        signal_source=source,
        signal_binding_id=signal_binding_id,
        signal_state=signal_state,
        signal_observed_at=signal_observed_at,
        signal_release_generation=signal_release_generation,
        signal_vm_name=signal_vm_name,
        signal_vm_hostname=signal_vm_hostname,
        signal_caller=signal_caller,
    )
    return updated


def claim_binding_vm_assignment_attempt(
    custom_api: k8s_client.CustomObjectsApi,
    namespace: str,
    assistant_id: str,
    *,
    target_binding_id: str,
    stale_after_seconds: float,
    source: str | None = None,
) -> str | None:
    """Mark the current binding as having one in-flight VM assignment attempt."""

    attempt_id = uuid.uuid4().hex
    observed_at = datetime.now(timezone.utc).isoformat()

    def mutate(_current: dict[str, Any], current_binding: dict[str, Any]):
        if binding_vm_ref(current_binding):
            return _STATUS_UNSET
        current_assignment = binding_vm_assignment(current_binding)
        current_state = str(current_assignment.get("state", "") or "")
        current_age = _signal_dict_age_seconds(current_assignment)
        if current_state == "in_progress" and (
            current_age is None or current_age < stale_after_seconds
        ):
            return _STATUS_UNSET
        return _binding_from_current(
            current_binding,
            binding_id_override=target_binding_id,
            vm_assignment=build_binding_vm_assignment(
                attempt_id=attempt_id,
                observed_at=observed_at,
                state="in_progress",
            ),
        )

    updated = patch_assistant_session_status(
        custom_api,
        namespace,
        assistant_id,
        source=source,
        expected_binding_id=target_binding_id,
        require_desired_state=DESIRED_STATE_RUNNING,
        binding_mutator=mutate,
    )
    updated_binding = session_binding(updated)
    updated_assignment = binding_vm_assignment(updated_binding)
    if (
        binding_id(updated_binding) != target_binding_id
        or assistant_session_desired_state(updated) != DESIRED_STATE_RUNNING
        or binding_vm_ref(updated_binding)
    ):
        return None
    if (
        str(updated_assignment.get("state", "") or "") == "in_progress"
        and str(updated_assignment.get("attemptId", "") or "") == attempt_id
    ):
        return attempt_id
    return None


def persist_binding_vm_assignment_result(
    custom_api: k8s_client.CustomObjectsApi,
    namespace: str,
    assistant_id: str,
    *,
    target_binding_id: str,
    attempt_id: str,
    state: str,
    message: str | None = None,
    vm_ref: dict[str, Any] | None = None,
    source: str | None = None,
) -> bool:
    """Persist the outcome for the current binding VM assignment attempt."""

    observed_at = datetime.now(timezone.utc).isoformat()

    def mutate(_current: dict[str, Any], current_binding: dict[str, Any]):
        current_assignment = binding_vm_assignment(current_binding)
        if (
            str(current_assignment.get("state", "") or "") != "in_progress"
            or str(current_assignment.get("attemptId", "") or "") != attempt_id
        ):
            return _STATUS_UNSET
        if state == "assigned":
            if not isinstance(vm_ref, dict) or not vm_ref.get("name"):
                raise ValueError("Assigned VM result requires a vm_ref")
            return _binding_from_current(
                current_binding,
                binding_id_override=target_binding_id,
                vm_assignment=None,
                vm_ref=vm_ref,
                desktop_url=None,
                vm_assigned_at=observed_at,
                guest_handshake_started_at=observed_at,
                vm_ready_observed_at=None,
                vm_ready_hostname=None,
                vm_ready_message_id=None,
                release_requested_at=None,
                release_completed_at=None,
            )
        return _binding_from_current(
            current_binding,
            binding_id_override=target_binding_id,
            vm_assignment=build_binding_vm_assignment(
                attempt_id=attempt_id,
                observed_at=observed_at,
                state=state,
                message=message,
            ),
        )

    updated = patch_assistant_session_status(
        custom_api,
        namespace,
        assistant_id,
        source=source,
        expected_binding_id=target_binding_id,
        require_desired_state=DESIRED_STATE_RUNNING,
        binding_mutator=mutate,
    )
    updated_binding = session_binding(updated)
    if binding_id(updated_binding) != target_binding_id:
        return False
    if state == "assigned":
        return bool(binding_vm_ref(updated_binding).get("name")) and not bool(
            binding_vm_assignment(updated_binding),
        )
    updated_assignment = binding_vm_assignment(updated_binding)
    return (
        str(updated_assignment.get("attemptId", "") or "") == attempt_id
        and str(updated_assignment.get("state", "") or "") == state
    )


def _signal_dict_age_seconds(payload: dict[str, Any] | None) -> float | None:
    observed_at = str((payload or {}).get("observedAt", "") or "")
    if not observed_at:
        return None
    try:
        parsed = datetime.fromisoformat(observed_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    ).total_seconds()


def get_phase(session: dict[str, Any] | None) -> str:
    if not session:
        return ""
    return str(session.get("status", {}).get("phase", ""))


def is_terminal_phase(phase: str) -> bool:
    return phase in TERMINAL_PHASES


def is_active_phase(phase: str) -> bool:
    return phase in ACTIVE_PHASES
