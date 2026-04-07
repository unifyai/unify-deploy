from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import base64
import json
import logging
import os
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
SIGNAL_VM_ASSIGNMENT = "vmAssignment"
SIGNAL_DESKTOP_READY = "desktopReady"
SIGNAL_VM_GUEST_HEALTH = "vmGuestHealth"
SIGNAL_VM_RELEASE_REQUEST = "vmReleaseRequest"
SIGNAL_VM_RELEASE_COMPLETE = "vmReleaseComplete"
_STATUS_UNSET = object()
_MAX_CAS_RETRIES = 3
_ASSISTANT_SESSION_SPEC_CONVERGENCE_IGNORED_FIELDS = frozenset({"requestedAt"})


def _sanitize_for_k8s(value: str) -> str:
    return str(value).lower().replace("_", "-")


def assistant_session_name(assistant_id: str) -> str:
    return f"assistant-session-{_sanitize_for_k8s(assistant_id)}"


def assistant_session_secret_name(assistant_id: str) -> str:
    return f"assistant-session-bootstrap-{_sanitize_for_k8s(assistant_id)}"


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


def binding_desktop_url(binding: dict[str, Any] | None) -> str:
    """Return the resolved desktop URL stored on the binding."""

    return str((binding or {}).get("desktopUrl", "") or "")


def binding_id(binding: dict[str, Any] | None) -> str:
    """Return the immutable binding identifier."""

    return str((binding or {}).get("id", "") or "")


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


def resolve_current_binding_vm_ref(
    binding: dict[str, Any] | None,
    *,
    assignment_signal: dict[str, Any] | None = None,
    owned_runtime_vms: list[dict[str, Any]] | None = None,
    disk_vm_name: str | None = None,
) -> dict[str, Any]:
    """Return the unambiguous VM reference for the current binding.

    Release flows can run before a matching ``vmAssignment`` signal has been
    persisted into ``status.binding.vmRef``. Recover the current VM only when
    the binding-scoped evidence points to exactly one owner.
    """

    current_vm_ref = binding_vm_ref(binding)
    current_vm_name = str(current_vm_ref.get("name", "") or "")
    if current_vm_name:
        return current_vm_ref

    current_binding_id = binding_id(binding)
    signal_binding_id = str((assignment_signal or {}).get("bindingId", "") or "")
    if current_binding_id and signal_binding_id == current_binding_id:
        signaled_vm_ref = (assignment_signal or {}).get("vmRef")
        if isinstance(signaled_vm_ref, dict) and signaled_vm_ref.get("name"):
            return signaled_vm_ref

    candidates = owned_runtime_vms or []
    if len(candidates) != 1:
        return {}

    candidate = candidates[0]
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
) -> dict[str, Any]:
    """Build the canonical binding payload stored under ``status.binding``."""

    fields = {
        "id": binding_id,
        "jobRef": job_ref,
        "podRef": pod_ref,
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
    }
    return {key: value for key, value in fields.items() if value not in (None, "")}


def _binding_release_field_regressions(
    current_binding: dict[str, Any] | None,
    next_binding: dict[str, Any] | None,
) -> dict[str, dict[str, str | None]]:
    """Return release lifecycle fields that would be cleared on the same binding."""

    current_binding_id = binding_id(current_binding)
    next_binding_id = binding_id(next_binding)
    if not current_binding_id or current_binding_id != next_binding_id:
        return {}

    regressions: dict[str, dict[str, str | None]] = {}
    for field_name in ("releaseRequestedAt", "releaseCompletedAt"):
        current_value = str((current_binding or {}).get(field_name, "") or "")
        next_value = str((next_binding or {}).get(field_name, "") or "")
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
    vm_ref = binding_vm_ref(binding)

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
        "job_name": overrides.pop("job_name", binding_job_ref(binding).get("name")),
        "pod_name": overrides.pop("pod_name", binding_pod_ref(binding).get("name")),
        "vm_name": overrides.pop("vm_name", vm_ref.get("name")),
        "vm_hostname": overrides.pop(
            "vm_hostname",
            vm_ref.get("hostname"),
        ),
        "desktop_url": overrides.pop("desktop_url", binding_desktop_url(binding)),
        "last_error": overrides.pop("last_error", status.get("lastError")),
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


def _secret_matches_payload(secret, payload: dict[str, Any]) -> bool:
    try:
        return _secret_startup_payload(secret) == payload
    except Exception:
        return False


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
    payload: dict[str, Any],
) -> str:
    secret_name = assistant_session_secret_name(assistant_id)
    body = k8s_client.V1Secret(
        metadata=k8s_client.V1ObjectMeta(name=secret_name, namespace=namespace),
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
                secret_name=secret_name,
            )
            return secret_name
        except ApiException as e:
            if e.status != 409:
                raise
            emit_observability_event(
                "assistantsession.bootstrap_secret_create_conflict",
                assistant_id=assistant_id,
                secret_name=secret_name,
                error=str(e),
            )
            existing_secret = _read_secret_or_none(core_api, namespace, secret_name)
            if existing_secret is None:
                raise RuntimeError(
                    f"Bootstrap secret {secret_name} still missing after create conflict",
                )

    for _attempt in range(3):
        if _secret_matches_payload(existing_secret, payload):
            emit_observability_event(
                "assistantsession.bootstrap_secret_already_current",
                assistant_id=assistant_id,
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
    if final_secret is not None and _secret_matches_payload(final_secret, payload):
        emit_observability_event(
            "assistantsession.bootstrap_secret_converged_after_conflicts",
            assistant_id=assistant_id,
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
    signals: dict[str, Any] | None | object = _STATUS_UNSET,
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
        if phase is not None:
            next_status["phase"] = phase
        if observed_activation_id is not None:
            next_status["observedActivationId"] = observed_activation_id
        if binding is not _STATUS_UNSET:
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
        if signals is not _STATUS_UNSET:
            next_status["signals"] = deepcopy(signals)

        next_binding = session_binding({"status": next_status})
        current_binding_id = binding_id(current_binding)
        next_binding_id = binding_id(next_binding)
        if (
            binding is not _STATUS_UNSET
            and current_binding_id != next_binding_id
            and signals is _STATUS_UNSET
        ):
            next_status["signals"] = {}
        release_field_regressions = (
            _binding_release_field_regressions(current_binding, next_binding)
            if binding is not _STATUS_UNSET
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
    if stored_payload is not None:
        signal_binding_id = stored_payload.get("bindingId")
        signal_state = stored_payload.get("state")
        signal_observed_at = stored_payload.get("observedAt")
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
        signal_vm_name=signal_vm_name,
        signal_vm_hostname=signal_vm_hostname,
        signal_caller=signal_caller,
    )
    return updated


def get_phase(session: dict[str, Any] | None) -> str:
    if not session:
        return ""
    return str(session.get("status", {}).get("phase", ""))


def is_terminal_phase(phase: str) -> bool:
    return phase in TERMINAL_PHASES


def is_active_phase(phase: str) -> bool:
    return phase in ACTIVE_PHASES
