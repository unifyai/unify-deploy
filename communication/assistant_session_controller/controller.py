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
    ACTIVE_PHASES,
    assistant_session_observability_fields,
    CONTAINER_READY_ANNOTATION,
    SESSION_REF_ANNOTATION,
    SESSION_REF_LABEL,
    build_condition,
    desktop_url_matches_vm_ref,
    emit_observability_event,
    get_latest_unity_image,
    merge_conditions,
    patch_assistant_session_status,
    read_bootstrap_secret,
    vm_refs_match,
)
from communication.infra.helpers import create_unity_job
from communication.infra.vm_helpers import (
    assign_pool_vm,
    get_assigned_vm_ref,
    release_pool_vm,
    replenish_pool,
)

logger = logging.getLogger(__name__)

WATCH_NAMESPACE = os.environ.get("WATCH_NAMESPACE", SETTINGS.default_namespace)
RECONCILE_INTERVAL_SECONDS = float(os.environ.get("SESSION_RECONCILE_INTERVAL", "5"))

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


def _priority_class_name() -> str:
    return "unity-critical" if SETTINGS.deploy_env == "production" else "unity-high"


def _conditions_map(conditions: list[dict] | None) -> dict[str, dict]:
    return {c.get("type", ""): c for c in (conditions or []) if c.get("type")}


def _condition_is_true(conditions: list[dict] | None, condition_type: str) -> bool:
    return _conditions_map(conditions).get(condition_type, {}).get("status") == "True"


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


def _session_jobs(label_selector: str) -> list:
    assert _batch_api is not None
    jobs = _batch_api.list_namespaced_job(
        namespace=WATCH_NAMESPACE,
        label_selector=label_selector,
    )
    return list(jobs.items)


def _active_job_for_session(session_name: str):
    jobs = _session_jobs(f"{SESSION_REF_LABEL}={session_name}")
    for job in jobs:
        if (
            job.status.active
            and job.status.active > 0
            and not job.metadata.deletion_timestamp
        ):
            return job
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


def _claim_idle_job(assistant_id: str, session_name: str):
    assert _batch_api is not None
    sanitized = _sanitize_for_k8s(assistant_id)
    jobs = _batch_api.list_namespaced_job(
        namespace=WATCH_NAMESPACE,
        label_selector="app=unity,unity-status=idle",
    )
    for job in jobs.items:
        if not job.status.active or job.status.active <= 0:
            continue
        labels = dict(job.metadata.labels or {})
        labels["assistant-id"] = sanitized
        labels["unity-status"] = "running"
        labels[SESSION_REF_LABEL] = session_name
        annotations = dict(job.metadata.annotations or {})
        annotations[SESSION_REF_ANNOTATION] = session_name
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
            logger.info(
                "Claimed idle job %s for assistant %s session %s",
                job.metadata.name,
                assistant_id,
                session_name,
            )
            emit_observability_event(
                "controller.job_claimed",
                assistant_id=assistant_id,
                session_name=session_name,
                job_name=job.metadata.name,
                source="controller.reconcile",
            )
            return _batch_api.read_namespaced_job(
                name=job.metadata.name,
                namespace=WATCH_NAMESPACE,
            )
        except ApiException as e:
            if e.status == 409:
                continue
            raise
    return None


def _create_session_bound_job(assistant_id: str, session_name: str):
    assert _batch_api is not None
    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")
    random_id = f"u{uuid.uuid4().hex[:4]}"
    job_name = f"unity-{timestamp_str}-{random_id}{SETTINGS.env_suffix}"
    image = get_latest_unity_image()
    extra_labels = {
        "assistant-id": _sanitize_for_k8s(assistant_id),
        "unity-status": "running",
        SESSION_REF_LABEL: session_name,
    }
    extra_annotations = {
        SESSION_REF_ANNOTATION: session_name,
        CONTAINER_READY_ANNOTATION: "false",
    }
    created = create_unity_job(
        batch_api=_batch_api,
        job_name=job_name,
        namespace=WATCH_NAMESPACE,
        image=image,
        deploy_env=SETTINGS.deploy_env,
        unity_status="running",
        priority_class_name=_priority_class_name(),
        extra_labels=extra_labels,
        extra_annotations=extra_annotations,
    )
    if not created:
        return None
    emit_observability_event(
        "controller.session_bound_job_created",
        assistant_id=assistant_id,
        session_name=session_name,
        job_name=job_name,
        source="controller.reconcile",
    )
    return _batch_api.read_namespaced_job(name=job_name, namespace=WATCH_NAMESPACE)


def _ensure_job_binding(assistant_id: str, session_name: str):
    job = _active_job_for_session(session_name)
    if job is not None:
        return job

    # Opportunistically adopt a single already-running legacy job for the assistant.
    legacy_jobs = _session_jobs(
        f"app=unity,assistant-id={_sanitize_for_k8s(assistant_id)}",
    )
    active_legacy = [
        job
        for job in legacy_jobs
        if job.status.active
        and job.status.active > 0
        and not job.metadata.deletion_timestamp
    ]
    if len(active_legacy) == 1:
        job = active_legacy[0]
        labels = dict(job.metadata.labels or {})
        labels[SESSION_REF_LABEL] = session_name
        annotations = dict(job.metadata.annotations or {})
        annotations[SESSION_REF_ANNOTATION] = session_name
        annotations.setdefault(CONTAINER_READY_ANNOTATION, "true")
        body = {
            "metadata": {
                "labels": labels,
                "annotations": annotations,
                "resourceVersion": job.metadata.resource_version,
            },
        }
        _batch_api.patch_namespaced_job(
            name=job.metadata.name,
            namespace=WATCH_NAMESPACE,
            body=body,
        )
        emit_observability_event(
            "controller.legacy_job_adopted",
            assistant_id=assistant_id,
            session_name=session_name,
            job_name=job.metadata.name,
            source="controller.reconcile",
        )
        return _batch_api.read_namespaced_job(
            name=job.metadata.name,
            namespace=WATCH_NAMESPACE,
        )

    job = _claim_idle_job(assistant_id, session_name)
    if job is not None:
        return job
    return _create_session_bound_job(assistant_id, session_name)


def _update_status_for_session(body: dict) -> None:
    assert _custom_api is not None
    session_name = body["metadata"]["name"]
    spec = body.get("spec", {})
    status = body.get("status", {})

    assistant_id = str(spec.get("assistantId", ""))
    activation_id = str(spec.get("activationId", ""))
    desktop_required = bool(spec.get("desktopRequired", False))
    desktop_mode = str(spec.get("desktopMode", ""))
    secret_name = str(spec.get("startupSecretRef", ""))
    observed_activation_id = str(status.get("observedActivationId", ""))
    new_activation = observed_activation_id != activation_id
    existing_conditions = [] if new_activation else status.get("conditions", [])
    emit_observability_event(
        "controller.session_reconcile",
        **assistant_session_observability_fields(
            body,
            source="controller.reconcile",
            new_activation=new_activation,
            observed_activation_id=observed_activation_id,
        ),
    )

    if not assistant_id or not activation_id or not secret_name:
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="Failed",
            job_ref=None,
            pod_ref=None,
            vm_ref=None,
            desktop_url=None,
            last_error="AssistantSession missing required spec fields",
            source="controller.reconcile",
            conditions=merge_conditions(
                existing_conditions,
                build_condition(
                    "Active",
                    False,
                    "InvalidSpec",
                    "Missing required spec",
                ),
            ),
        )
        return

    job = _ensure_job_binding(assistant_id, session_name)
    if job is None:
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="Failed",
            observed_activation_id=activation_id,
            job_ref=None,
            pod_ref=None,
            vm_ref=None,
            desktop_url=None,
            last_error="Failed to bind a Unity job",
            source="controller.reconcile",
            conditions=merge_conditions(
                existing_conditions,
                build_condition(
                    "ContainerAssigned",
                    False,
                    "BindFailed",
                    "No Unity job could be bound",
                ),
            ),
        )
        return

    terminal_phase = _job_terminal_phase(job)
    job_ref = {"name": job.metadata.name, "namespace": WATCH_NAMESPACE}
    pod_ref = _current_pod_ref(job.metadata.name)
    conditions = merge_conditions(
        existing_conditions,
        build_condition("ContainerAssigned", True, "Bound", "Session job bound"),
    )

    if terminal_phase:
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase=terminal_phase,
            observed_activation_id=activation_id,
            job_ref=job_ref,
            pod_ref=pod_ref,
            vm_ref=None,
            desktop_url=None,
            source="controller.reconcile",
            conditions=merge_conditions(
                conditions,
                build_condition(
                    "Active",
                    False,
                    "Terminal",
                    f"Job reached terminal phase {terminal_phase}",
                ),
            ),
        )
        return

    annotations = job.metadata.annotations or {}
    container_ready = annotations.get(CONTAINER_READY_ANNOTATION) == "true"
    if not container_ready:
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="ContainerAssigned",
            observed_activation_id=activation_id,
            job_ref=job_ref,
            pod_ref=pod_ref,
            vm_ref=None if new_activation else status.get("vmRef"),
            desktop_url=None if new_activation else status.get("desktopUrl"),
            last_error="" if new_activation else None,
            source="controller.reconcile",
            conditions=merge_conditions(
                conditions,
                build_condition(
                    "ContainerReady",
                    False,
                    "WaitingForUnity",
                    "Unity has not yet signaled container-ready",
                ),
                build_condition(
                    "Active",
                    False,
                    "WaitingForUnity",
                    "Waiting for Unity bootstrap",
                ),
            ),
        )
        return

    conditions = merge_conditions(
        conditions,
        build_condition(
            "ContainerReady",
            True,
            "UnityReady",
            "Unity bootstrap complete",
        ),
    )

    if not desktop_required:
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="Active",
            observed_activation_id=activation_id,
            job_ref=job_ref,
            pod_ref=pod_ref,
            vm_ref=None,
            desktop_url=None,
            last_error="" if new_activation else None,
            source="controller.reconcile",
            conditions=merge_conditions(
                conditions,
                build_condition("Active", True, "Ready", "Container session active"),
            ),
        )
        return

    vm_ref = None if new_activation else status.get("vmRef")
    desktop_url = None if new_activation else status.get("desktopUrl")
    assigned_vm_ref = None if new_activation else get_assigned_vm_ref(assistant_id)
    if assigned_vm_ref is None:
        vm_ref = None
        desktop_url = None
        existing_conditions = merge_conditions(
            existing_conditions,
            build_condition(
                "DesktopReady",
                False,
                "WaitingForDesktop",
                "Waiting for authenticated desktop readiness",
            ),
            build_condition(
                "Active",
                False,
                "WaitingForDesktop",
                "Desktop session not ready yet",
            ),
        )
    else:
        if not vm_refs_match(vm_ref, assigned_vm_ref):
            vm_ref = assigned_vm_ref
            desktop_url = None
            existing_conditions = merge_conditions(
                existing_conditions,
                build_condition(
                    "DesktopReady",
                    False,
                    "WaitingForDesktop",
                    "Waiting for authenticated desktop readiness",
                ),
                build_condition(
                    "Active",
                    False,
                    "WaitingForDesktop",
                    "Desktop session not ready yet",
                ),
            )
        elif desktop_url and not desktop_url_matches_vm_ref(
            desktop_url,
            assigned_vm_ref,
        ):
            desktop_url = None
            existing_conditions = merge_conditions(
                existing_conditions,
                build_condition(
                    "DesktopReady",
                    False,
                    "WaitingForDesktop",
                    "Waiting for authenticated desktop readiness",
                ),
                build_condition(
                    "Active",
                    False,
                    "WaitingForDesktop",
                    "Desktop session not ready yet",
                ),
            )

    if not vm_ref:
        startup_payload = read_bootstrap_secret(_core_api, WATCH_NAMESPACE, secret_name)
        api_key = str(startup_payload.get("api_key", ""))
        try:
            result = assign_pool_vm(
                assistant_id=assistant_id,
                unify_apikey=api_key,
                vm_type=desktop_mode or "ubuntu",
            )
            vm_ref = {
                "name": result["vm_name"],
                "hostname": result["hostname"],
                "vmType": desktop_mode or "ubuntu",
            }
            conditions = merge_conditions(
                conditions,
                build_condition("VMAssigned", True, "Assigned", "Managed VM assigned"),
                build_condition(
                    "DesktopReady",
                    False,
                    "WaitingForDesktop",
                    "Waiting for authenticated desktop readiness",
                ),
                build_condition(
                    "Active",
                    False,
                    "WaitingForDesktop",
                    "Desktop session not ready yet",
                ),
            )
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="PendingVM",
                observed_activation_id=activation_id,
                job_ref=job_ref,
                pod_ref=pod_ref,
                vm_ref=vm_ref,
                desktop_url=None,
                last_error="",
                source="controller.reconcile",
                conditions=conditions,
            )
            return
        except ValueError as exc:
            replenish_pool(desktop_mode or "ubuntu")
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="PendingVM",
                observed_activation_id=activation_id,
                job_ref=job_ref,
                pod_ref=pod_ref,
                vm_ref=None,
                desktop_url=None,
                last_error=str(exc),
                source="controller.reconcile",
                conditions=merge_conditions(
                    conditions,
                    build_condition(
                        "VMAssigned",
                        False,
                        "WaitingForCapacity",
                        str(exc),
                    ),
                    build_condition(
                        "DesktopReady",
                        False,
                        "WaitingForCapacity",
                        "Waiting for VM capacity",
                    ),
                ),
            )
            return
        except Exception as exc:  # pragma: no cover - defensive reconcile
            logger.exception("AssistantSession VM assignment failed")
            emit_observability_event(
                "controller.vm_assignment_failed",
                **assistant_session_observability_fields(
                    body,
                    source="controller.reconcile",
                    error=str(exc),
                ),
            )
            patch_assistant_session_status(
                _custom_api,
                WATCH_NAMESPACE,
                assistant_id,
                phase="PendingVM",
                observed_activation_id=activation_id,
                job_ref=job_ref,
                pod_ref=pod_ref,
                vm_ref=None,
                desktop_url=None,
                last_error=str(exc),
                source="controller.reconcile",
                conditions=merge_conditions(
                    conditions,
                    build_condition(
                        "VMAssigned",
                        False,
                        "AssignError",
                        str(exc),
                    ),
                ),
            )
            return

    if _condition_is_true(existing_conditions, "DesktopReady"):
        patch_assistant_session_status(
            _custom_api,
            WATCH_NAMESPACE,
            assistant_id,
            phase="Active",
            observed_activation_id=activation_id,
            job_ref=job_ref,
            pod_ref=pod_ref,
            vm_ref=vm_ref,
            desktop_url=desktop_url,
            last_error="",
            source="controller.reconcile",
            conditions=merge_conditions(
                conditions,
                build_condition("VMAssigned", True, "Assigned", "Managed VM assigned"),
                build_condition(
                    "DesktopReady",
                    True,
                    "DesktopReady",
                    "Authenticated desktop readiness complete",
                ),
                build_condition("Active", True, "Ready", "Desktop session active"),
            ),
        )
        return

    patch_assistant_session_status(
        _custom_api,
        WATCH_NAMESPACE,
        assistant_id,
        phase="PendingVM",
        observed_activation_id=activation_id,
        job_ref=job_ref,
        pod_ref=pod_ref,
        vm_ref=vm_ref,
        desktop_url=desktop_url,
        last_error="",
        source="controller.reconcile",
        conditions=merge_conditions(
            conditions,
            build_condition("VMAssigned", True, "Assigned", "Managed VM assigned"),
            build_condition(
                "DesktopReady",
                False,
                "WaitingForDesktop",
                "Waiting for authenticated desktop readiness",
            ),
            build_condition(
                "Active",
                False,
                "WaitingForDesktop",
                "Desktop session not ready yet",
            ),
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
    phase = str(body.get("status", {}).get("phase", ""))
    if phase in ACTIVE_PHASES:
        _update_status_for_session(body)


@kopf.on.delete(
    SETTINGS.assistant_session_group,
    SETTINGS.assistant_session_version,
    SETTINGS.assistant_session_plural,
)
def delete_session(body, **_):
    assert _core_api is not None
    spec = body.get("spec", {})
    status = body.get("status", {})
    assistant_id = str(spec.get("assistantId", ""))
    secret_name = spec.get("startupSecretRef")
    if secret_name:
        try:
            _core_api.delete_namespaced_secret(
                name=secret_name,
                namespace=WATCH_NAMESPACE,
            )
        except ApiException as e:
            if e.status != 404:
                raise
    if assistant_id and status.get("vmRef"):
        try:
            release_pool_vm(assistant_id)
        except Exception:  # pragma: no cover - best effort cleanup
            logger.exception("Failed releasing VM for deleted AssistantSession")


@kopf.on.probe(id="health")
def health_probe(**_):
    return {"ts": _now_iso(), "namespace": WATCH_NAMESPACE}
