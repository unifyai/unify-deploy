from __future__ import annotations

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

logger = logging.getLogger(__name__)

SESSION_REF_LABEL = "assistantsession.unify.ai/name"
SESSION_REF_ANNOTATION = "assistantsession.unify.ai/name"
CONTAINER_READY_ANNOTATION = "assistantsession.unify.ai/container-ready"

TERMINAL_PHASES = {"Succeeded", "Failed"}
ACTIVE_PHASES = {"PendingContainer", "ContainerAssigned", "PendingVM", "Active"}
_STATUS_UNSET = object()
_MAX_CAS_RETRIES = 3
_ASSISTANT_SESSION_SPEC_CONVERGENCE_IGNORED_FIELDS = frozenset({"requestedAt"})


def _sanitize_for_k8s(value: str) -> str:
    return str(value).lower().replace("_", "-")


def assistant_session_name(assistant_id: str) -> str:
    return f"assistant-session-{_sanitize_for_k8s(assistant_id)}"


def assistant_session_secret_name(assistant_id: str) -> str:
    return f"assistant-session-bootstrap-{_sanitize_for_k8s(assistant_id)}"


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

    fields = {
        "assistant_id": overrides.pop("assistant_id", spec.get("assistantId")),
        "session_name": overrides.pop("session_name", metadata.get("name")),
        "activation_id": overrides.pop("activation_id", spec.get("activationId")),
        "observed_activation_id": overrides.pop(
            "observed_activation_id",
            status.get("observedActivationId"),
        ),
        "phase": overrides.pop("phase", status.get("phase")),
        "job_name": overrides.pop("job_name", (status.get("jobRef") or {}).get("name")),
        "pod_name": overrides.pop("pod_name", (status.get("podRef") or {}).get("name")),
        "vm_name": overrides.pop("vm_name", (status.get("vmRef") or {}).get("name")),
        "vm_hostname": overrides.pop(
            "vm_hostname",
            (status.get("vmRef") or {}).get("hostname"),
        ),
        "desktop_url": overrides.pop("desktop_url", status.get("desktopUrl")),
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
            "assistantsession.deleted",
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
) -> dict[str, Any]:
    desktop_required = desktop_mode in ("windows", "ubuntu")
    return {
        "assistantId": str(assistant_id),
        "userId": str(user_id),
        "medium": medium,
        "desktopRequired": desktop_required,
        "desktopMode": desktop_mode,
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
                    raise
                continue

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
    if _assistant_session_spec_matches(existing, spec):
        return existing
    if last_conflict is not None:
        raise last_conflict
    raise RuntimeError(
        f"AssistantSession {name} did not converge to the requested spec",
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
    job_ref: dict[str, Any] | None | object = _STATUS_UNSET,
    pod_ref: dict[str, Any] | None | object = _STATUS_UNSET,
    vm_ref: dict[str, Any] | None | object = _STATUS_UNSET,
    desktop_url: str | None | object = _STATUS_UNSET,
    last_error: str | None | object = _STATUS_UNSET,
    conditions: list[dict[str, Any]] | None | object = _STATUS_UNSET,
    source: str | None = None,
    bootstrap_retries: int | None | object = _STATUS_UNSET,
    vm_retries: int | None | object = _STATUS_UNSET,
    desktop_probe_failures: int | None | object = _STATUS_UNSET,
) -> dict[str, Any]:
    name = assistant_session_name(assistant_id)
    status_fields: dict[str, Any] = {}
    if phase is not None:
        status_fields["phase"] = phase
    if observed_activation_id is not None:
        status_fields["observedActivationId"] = observed_activation_id
    if job_ref is not _STATUS_UNSET:
        status_fields["jobRef"] = job_ref
    if pod_ref is not _STATUS_UNSET:
        status_fields["podRef"] = pod_ref
    if vm_ref is not _STATUS_UNSET:
        status_fields["vmRef"] = vm_ref
    if desktop_url is not _STATUS_UNSET:
        status_fields["desktopUrl"] = desktop_url
    if last_error is not _STATUS_UNSET:
        status_fields["lastError"] = last_error
    if conditions is not _STATUS_UNSET:
        status_fields["conditions"] = conditions
    if bootstrap_retries is not _STATUS_UNSET:
        status_fields["bootstrapRetries"] = bootstrap_retries
    if vm_retries is not _STATUS_UNSET:
        status_fields["vmRetries"] = vm_retries
    if desktop_probe_failures is not _STATUS_UNSET:
        status_fields["desktopProbeFailures"] = desktop_probe_failures

    current: dict[str, Any] = {}
    for _attempt in range(_MAX_CAS_RETRIES):
        current = get_assistant_session(custom_api, namespace, assistant_id) or {}
        current_status = current.get("status", {})
        if all(
            current_status.get(key) == value for key, value in status_fields.items()
        ):
            return current

        rv = current.get("metadata", {}).get("resourceVersion")
        body: dict[str, Any] = {"status": status_fields}
        if rv:
            body["metadata"] = {"resourceVersion": rv}
        try:
            result = custom_api.patch_namespaced_custom_object_status(
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


def get_phase(session: dict[str, Any] | None) -> str:
    if not session:
        return ""
    return str(session.get("status", {}).get("phase", ""))


def is_terminal_phase(phase: str) -> bool:
    return phase in TERMINAL_PHASES


def is_active_phase(phase: str) -> bool:
    return phase in ACTIVE_PHASES
