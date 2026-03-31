from __future__ import annotations

from datetime import datetime, timezone
import base64
import json
import os
from typing import Any

from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

from common.settings import SETTINGS
from .helpers import setup_kubernetes_client

SESSION_REF_LABEL = "assistantsession.unify.ai/name"
SESSION_REF_ANNOTATION = "assistantsession.unify.ai/name"
CONTAINER_READY_ANNOTATION = "assistantsession.unify.ai/container-ready"

TERMINAL_PHASES = {"Succeeded", "Failed"}
ACTIVE_PHASES = {"PendingContainer", "ContainerAssigned", "PendingVM", "Active"}
_STATUS_UNSET = object()


def _sanitize_for_k8s(value: str) -> str:
    return str(value).lower().replace("_", "-")


def assistant_session_name(assistant_id: str) -> str:
    return f"assistant-session-{_sanitize_for_k8s(assistant_id)}"


def assistant_session_secret_name(assistant_id: str) -> str:
    return f"assistant-session-bootstrap-{_sanitize_for_k8s(assistant_id)}"


def get_latest_unity_image() -> str:
    from google.cloud import storage
    from google.oauth2.service_account import Credentials

    creds_json = json.loads(os.getenv("GCP_SA_KEY", "{}"))
    creds = Credentials.from_service_account_info(creds_json)
    storage_client = storage.Client(credentials=creds)
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
    existing_secret = None
    try:
        existing_secret = core_api.read_namespaced_secret(
            name=secret_name,
            namespace=namespace,
        )
    except ApiException as e:
        if e.status != 404:
            raise
    if existing_secret is None:
        try:
            core_api.create_namespaced_secret(namespace=namespace, body=body)
            return secret_name
        except ApiException as e:
            if e.status != 409:
                raise
            return secret_name

    body.metadata.resource_version = existing_secret.metadata.resource_version
    core_api.replace_namespaced_secret(
        name=secret_name,
        namespace=namespace,
        body=body,
    )
    return secret_name


def read_bootstrap_secret(
    core_api,
    namespace: str,
    secret_name: str,
) -> dict[str, Any]:
    secret = core_api.read_namespaced_secret(name=secret_name, namespace=namespace)
    data = secret.data or {}
    raw = data.get("startup.json", "")
    if not raw:
        return {}
    return json.loads(base64.b64decode(raw).decode("utf-8"))


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
            existing = get_assistant_session(custom_api, namespace, assistant_id)
            if existing is not None:
                return existing
            raise

    patch = {"spec": spec}
    return custom_api.patch_namespaced_custom_object(
        group=SETTINGS.assistant_session_group,
        version=SETTINGS.assistant_session_version,
        namespace=namespace,
        plural=SETTINGS.assistant_session_plural,
        name=name,
        body=patch,
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
) -> dict[str, Any]:
    name = assistant_session_name(assistant_id)
    body: dict[str, Any] = {"status": {}}
    status = body["status"]
    if phase is not None:
        status["phase"] = phase
    if observed_activation_id is not None:
        status["observedActivationId"] = observed_activation_id
    if job_ref is not _STATUS_UNSET:
        status["jobRef"] = job_ref
    if pod_ref is not _STATUS_UNSET:
        status["podRef"] = pod_ref
    if vm_ref is not _STATUS_UNSET:
        status["vmRef"] = vm_ref
    if desktop_url is not _STATUS_UNSET:
        status["desktopUrl"] = desktop_url
    if last_error is not _STATUS_UNSET:
        status["lastError"] = last_error
    if conditions is not _STATUS_UNSET:
        status["conditions"] = conditions

    current = get_assistant_session(custom_api, namespace, assistant_id) or {}
    current_status = current.get("status", {})
    if all(current_status.get(key) == value for key, value in status.items()):
        return current

    return custom_api.patch_namespaced_custom_object_status(
        group=SETTINGS.assistant_session_group,
        version=SETTINGS.assistant_session_version,
        namespace=namespace,
        plural=SETTINGS.assistant_session_plural,
        name=name,
        body=body,
    )


def get_phase(session: dict[str, Any] | None) -> str:
    if not session:
        return ""
    return str(session.get("status", {}).get("phase", ""))


def is_terminal_phase(phase: str) -> bool:
    return phase in TERMINAL_PHASES


def is_active_phase(phase: str) -> bool:
    return phase in ACTIVE_PHASES
