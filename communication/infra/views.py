import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from functools import partial
from google.api_core.exceptions import Conflict, NotFound as GcpNotFound
from google.cloud import compute_v1, pubsub_v1, storage
from google.protobuf import duration_pb2
import json
import logging
import os
import time
from typing import Any
import uuid
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from common.int_list_codec import normalize_int_list
from common.team_summaries_codec import decode_team_summaries_from_form
from .helpers import (
    acquire_named_lease,
    create_unity_job,
    delete_job,
    get_job_logs,
    patch_job_labels,
    release_named_lease,
    suspend_job,
)
from .runtime_clients import (
    get_k8s_clients as _get_k8s_clients,
    get_pubsub_clients as _get_pubsub_clients,
    service_account_credentials as _service_account_credentials,
)
from .task_activation import router as task_activation_router
from .dashboard_actions import router as dashboard_actions_router
from .assistant_sessions import (
    ACTIVE_PHASES,
    AssistantSessionTerminatingError,
    BINDING_ID_LABEL as SESSION_BINDING_ID_LABEL,
    BINDING_ID_ANNOTATION,
    DESIRED_STATE_STOPPED,
    DESIRED_STATE_RUNNING,
    SIGNAL_DESKTOP_READY,
    SIGNAL_VM_RELEASE_COMPLETE,
    SESSION_REF_ANNOTATION,
    SESSION_REF_LABEL,
    SUSPEND_INTENT_STOP,
    TERMINAL_PHASES,
    assistant_session_desired_state,
    assistant_session_is_terminating,
    assistant_session_observability_fields,
    assistant_session_name,
    assistant_session_stop_requested,
    binding_desktop_url,
    binding_id as binding_id_from_status,
    binding_release_generation,
    binding_job_ref,
    binding_pod_ref,
    binding_vm_ref,
    build_assistant_session_spec,
    build_binding_signal,
    build_suspend_intent,
    create_or_update_assistant_session,
    create_or_update_bootstrap_secret,
    delete_assistant_session,
    delete_bootstrap_secret_if_owned,
    emit_observability_event,
    get_assistant_session,
    get_custom_objects_api,
    patch_assistant_session_spec,
    patch_assistant_session_status,
    read_bootstrap_secret,
    record_assistant_session_signal,
    released_binding,
    resolve_current_binding_vm_ref,
    session_binding,
    session_desktop_mode,
    session_desktop_required,
    vm_refs_match,
)
from .idle_job_pool import schedule_idle_job_pool_replenishment
from .observability import (
    build_causal_context,
    pop_causal_context,
    push_causal_context,
)
from .vm_helpers import (
    AssistantDiskInUseError,
    complete_pool_vm_release,
    get_dns_hostname,
    probe_vm_agent_service_authenticated,
    verify_vm_assignment,
    _set_pool_labels,
    _update_instance_metadata,
    provision_pool_vm,
    start_pool_vm,
    assign_pool_vm,
    release_pool_vm,
    replenish_pool,
    rebalance_pool,
    list_pool_vms,
    find_vm_with_disk,
    split_binding_runtime_vms,
    detach_assistant_disk,
    delete_assistant_disk,
    reconcile_orphaned_disks,
)
from .tunnel_helpers import (
    register_tunnel,
    unregister_tunnel,
    get_tunnel_status,
    list_user_tunnels,
)
from .models import (
    VMReadyRequest,
    VMReleaseCompleteRequest,
    VMWipeMetadataKeyRequest,
    TunnelRegisterRequest,
    TunnelRegisterResponse,
    TunnelStatusResponse,
    TunnelListResponse,
    TunnelDeleteResponse,
    PoolProvisionRequest,
    PoolStartVMRequest,
    PoolAssignRequest,
    PoolAssignResponse,
    PoolReleaseRequest,
    PoolStatusResponse,
    PoolVMStatus,
)
from common.settings import SETTINGS
from communication.dependencies import (
    authenticate_user_api_key,
    authenticate_vm_identity,
    extract_api_key,
)

logger = logging.getLogger(__name__)

DEFAULT_UNITY_IMAGE = f"{SETTINGS.image_registry}/{SETTINGS.unity_image_name}:latest"
TERMINAL_SESSION_PRUNE_DEFAULT_LIMIT = 50
TERMINAL_SESSION_PRUNE_MAX_LIMIT = 200
TERMINAL_SESSION_PRUNE_DEFAULT_RETENTION_HOURS = 24.0
TERMINAL_SESSION_GHOST_HEAL_GRACE_MINUTES = 10.0

ASSIGN_EXECUTOR = ThreadPoolExecutor(max_workers=15, thread_name_prefix="vm-assign")
POOL_MAINTENANCE_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="pool-maint",
)
START_JOB_LEASE_DURATION_SECONDS = SETTINGS.lease_duration_seconds
START_JOB_LEASE_WAIT_TIMEOUT_SECONDS = START_JOB_LEASE_DURATION_SECONDS + 5
START_JOB_LEASE_POLL_INTERVAL_SECONDS = 0.2
START_JOB_TERMINATING_SESSION_WAIT_TIMEOUT_SECONDS = 5.0
TASK_DUE_EVENT_TYPE = "task_due"
ASSISTANT_SESSION_CONTROLLER_DEPLOYMENTS = {
    "staging": "assistant-session-controller-staging",
}


async def _publish_desktop_ready(
    assistant_id: str,
    hostname: str,
    vm_type: str,
    *,
    binding_id: str,
) -> str:
    """Publish an ``assistant_desktop_ready`` system event via Pub/Sub.

    Publishes a single inbound message for Unity. Unity's event handler
    constructs the correct liveview URL (with ``/desktop/custom.html``)
    and re-publishes to the ``assistant_desktop_ready`` thread that
    Console's SSE subscription listens on.

    Returns the Pub/Sub message ID.
    """
    publisher, _ = await asyncio.to_thread(_get_pubsub_clients)
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = publisher.topic_path(SETTINGS.gcp_project_id, topic_name)

    message_data = json.dumps(
        {
            "thread": "unity_system_event",
            "publish_timestamp": time.time(),
            "event": {
                "assistant_id": assistant_id,
                "binding_id": binding_id,
                "event_type": "assistant_desktop_ready",
                "desktop_url": f"https://{hostname}",
                "vm_type": vm_type,
                "message": f"VM ({vm_type}) startup complete",
            },
        },
    ).encode("utf-8")

    future = publisher.publish(topic_path, data=message_data, thread="inbound")
    message_id = await asyncio.to_thread(future.result)
    logger.info(
        f"Published assistant_desktop_ready for assistant {assistant_id} "
        f"(message_id={message_id})",
    )
    return message_id


router = APIRouter()
router.include_router(task_activation_router)
router.include_router(dashboard_actions_router)


def _parse_desktop_required_form(
    raw_desktop_required: str,
    desktop_mode: str,
) -> bool | None:
    """Parse optional desktop_required override from /infra/job/start form data."""

    cleaned = str(raw_desktop_required or "").strip().lower()
    if not cleaned:
        return None
    if cleaned in {"true", "1", "yes"}:
        return True
    if cleaned in {"false", "0", "no"}:
        return False
    raise HTTPException(
        status_code=400,
        detail="desktop_required must be true or false when provided",
    )


def _parse_wake_reasons(raw_wake_reasons: str) -> list[dict[str, Any]]:
    """Parse the optional JSON-encoded wake reason list."""

    if not raw_wake_reasons:
        return []
    try:
        parsed = json.loads(raw_wake_reasons)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="wake_reasons must be valid JSON",
        ) from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, dict) for item in parsed
    ):
        raise HTTPException(
            status_code=400,
            detail="wake_reasons must be a JSON list of objects",
        )
    return parsed


def _decode_team_ids_form(team_ids: str) -> list[int]:
    """Decode the shared-team membership list carried by the start-job form."""

    if not team_ids:
        return []
    try:
        parsed = json.loads(team_ids)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail="team_ids must be valid JSON",
        ) from exc
    try:
        return normalize_int_list(parsed, field_name="team_ids")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _decode_team_summaries_form(team_summaries: str) -> list[dict[str, int | str]]:
    """Decode shared-team summaries carried by the start-job form."""

    try:
        return decode_team_summaries_from_form(
            team_summaries,
            field_name="team_summaries",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _startup_payload_without_ephemeral_wake_reasons(
    payload: dict[str, Any],
    *,
    keep_wake_reasons: bool,
) -> dict[str, Any]:
    """Drop one-shot wake reasons when reusing an already active session."""

    if keep_wake_reasons:
        return payload
    return {key: value for key, value in payload.items() if key != "wake_reasons"}


def _start_job_lease_name(assistant_id: str) -> str:
    """Return the Lease name used to serialize `/infra/job/start` writers."""
    return f"assistant-start-{str(assistant_id).lower().replace('_', '-')}"


async def _acquire_start_job_lease(
    coord_api,
    assistant_id: str,
    namespace: str,
) -> tuple[str, str, int]:
    """Acquire the per-assistant start-intent lease.

    Duplicate `/infra/job/start` calls must serialize before minting or
    returning an activation, otherwise concurrent writers can each return a
    different activation while only one `AssistantSession` survives.
    """
    lease_name = _start_job_lease_name(assistant_id)
    holder_id = f"job-start-{assistant_id}-{uuid.uuid4().hex[:8]}"
    wait_started = time.monotonic()
    deadline = wait_started + START_JOB_LEASE_WAIT_TIMEOUT_SECONDS

    while True:
        acquired = await asyncio.to_thread(
            acquire_named_lease,
            coord_api,
            lease_name,
            namespace,
            holder_id,
            START_JOB_LEASE_DURATION_SECONDS,
        )
        if acquired:
            return lease_name, holder_id, int((time.monotonic() - wait_started) * 1000)
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out waiting for start lease for assistant {assistant_id}",
            )
        await asyncio.sleep(START_JOB_LEASE_POLL_INTERVAL_SECONDS)


async def _load_startable_assistant_session(
    custom_api,
    assistant_id: str,
) -> dict | None:
    """Return the latest session snapshot after waiting out delete finalization."""

    session = await asyncio.to_thread(
        get_assistant_session,
        custom_api,
        SETTINGS.default_namespace,
        assistant_id,
    )
    if not assistant_session_is_terminating(session):
        return session

    deadline = time.monotonic() + START_JOB_TERMINATING_SESSION_WAIT_TIMEOUT_SECONDS
    while assistant_session_is_terminating(session) and time.monotonic() < deadline:
        await asyncio.sleep(START_JOB_LEASE_POLL_INTERVAL_SECONDS)
        session = await asyncio.to_thread(
            get_assistant_session,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
        )
    return session


def _safe_int(value: object) -> int:
    """Best-effort integer coercion for Kubernetes status fields."""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _assistant_session_controller_deployment_name() -> str:
    """Return the controller Deployment name for the current environment."""

    deploy_env = str(SETTINGS.deploy_env or "").lower()
    return ASSISTANT_SESSION_CONTROLLER_DEPLOYMENTS.get(
        deploy_env,
        "assistant-session-controller",
    )


async def _assistant_session_control_plane_ready(
    batch_api,
    custom_api,
) -> tuple[bool, str | None]:
    """Return whether new AssistantSession activations can safely proceed."""

    if custom_api is None or batch_api is None:
        return False, "assistant_session_api_unavailable"

    try:
        await asyncio.to_thread(
            custom_api.list_namespaced_custom_object,
            group=SETTINGS.assistant_session_group,
            version=SETTINGS.assistant_session_version,
            namespace=SETTINGS.default_namespace,
            plural=SETTINGS.assistant_session_plural,
            limit=1,
        )
    except ApiException as exc:
        return False, f"assistant_session_api_error_{exc.status}"

    apps_api = k8s_client.AppsV1Api(batch_api.api_client)
    controller_name = _assistant_session_controller_deployment_name()
    try:
        deployment = await asyncio.to_thread(
            apps_api.read_namespaced_deployment,
            name=controller_name,
            namespace=SETTINGS.default_namespace,
        )
    except ApiException as exc:
        if exc.status == 404:
            return False, "assistant_session_controller_missing"
        raise

    available_replicas = _safe_int(
        getattr(getattr(deployment, "status", None), "available_replicas", 0),
    )
    if available_replicas < 1:
        return False, "assistant_session_controller_unavailable"
    return True, None


async def _cleanup_superseded_bootstrap_secrets(
    *,
    core_api,
    assistant_id: str,
    existing_session: dict | None,
    attempted_refs: list[tuple[str, str]],
    final_secret_name: str,
) -> None:
    """Delete bootstrap Secrets that were superseded by a newer session ref."""

    cleanup_candidates: list[tuple[str, str]] = []
    if existing_session:
        cleanup_candidates.append(
            (
                str(existing_session.get("spec", {}).get("startupSecretRef", "") or ""),
                str(existing_session.get("spec", {}).get("activationId", "") or ""),
            ),
        )
    cleanup_candidates.extend(attempted_refs)

    seen: set[tuple[str, str]] = set()
    for secret_name, activation_id in cleanup_candidates:
        candidate = (str(secret_name or ""), str(activation_id or ""))
        if not candidate[0] or candidate[0] == final_secret_name or candidate in seen:
            continue
        seen.add(candidate)
        try:
            await asyncio.to_thread(
                delete_bootstrap_secret_if_owned,
                core_api,
                SETTINGS.default_namespace,
                assistant_id=assistant_id,
                activation_id=candidate[1],
                secret_name=candidate[0],
            )
        except Exception as exc:
            logger.exception(
                "Failed cleaning superseded bootstrap secret %s for assistant %s",
                candidate[0],
                assistant_id,
            )
            emit_observability_event(
                "infra.job_start.secret_cleanup_failed",
                assistant_id=assistant_id,
                activation_id=candidate[1] or None,
                secret_name=candidate[0],
                error=f"{type(exc).__name__}: {exc}",
            )


def _build_startup_payload(
    *,
    api_key: str,
    medium: str,
    assistant_id: str,
    user_id: str,
    user_first_name: str,
    user_surname: str,
    user_email: str,
    assistant_first_name: str,
    assistant_surname: str,
    assistant_age: str,
    assistant_nationality: str,
    assistant_about: str,
    assistant_timezone: str,
    user_number: str,
    assistant_number: str,
    assistant_email: str,
    assistant_email_provider: str,
    user_whatsapp_number: str,
    assistant_whatsapp_number: str,
    assistant_discord_bot_id: str,
    voice_provider: str,
    voice_id: str,
    desktop_mode: str,
    desktop_url: str,
    user_desktops: str,
    is_coordinator: str,
    demo_id: str,
    team_ids: str,
    team_summaries: str,
    self_contact_id: int,
    boss_contact_id: int,
    org_id: str,
    assistant_job_title: str = "",
    wake_reasons: list[dict[str, Any]] | None = None,
) -> dict:
    """Build the bootstrap Secret payload for a session activation request.

    `/infra/job/start` may reuse a non-terminal AssistantSession, so callers
    must always pass the latest assistant config. The bootstrap Secret becomes
    the controller's source of truth for both fresh and reused activations.
    """
    payload = {
        "api_key": api_key,
        "medium": medium,
        "assistant_id": assistant_id,
        "user_id": user_id,
        "user_first_name": user_first_name,
        "user_surname": user_surname,
        "user_email": user_email,
        "assistant_first_name": assistant_first_name,
        "assistant_surname": assistant_surname,
        "assistant_age": assistant_age,
        "assistant_nationality": assistant_nationality,
        "assistant_about": assistant_about,
        "assistant_job_title": assistant_job_title,
        "assistant_timezone": assistant_timezone,
        "user_number": user_number,
        "assistant_number": assistant_number,
        "assistant_email": assistant_email,
        "assistant_email_provider": assistant_email_provider,
        "user_whatsapp_number": user_whatsapp_number,
        "assistant_whatsapp_number": assistant_whatsapp_number,
        "assistant_discord_bot_id": assistant_discord_bot_id,
        "voice_provider": voice_provider,
        "voice_id": voice_id,
        "desktop_mode": desktop_mode,
        "desktop_url": desktop_url if desktop_url else None,
        "user_desktops": json.loads(user_desktops) if user_desktops else [],
        "is_coordinator": is_coordinator.lower() == "true",
        "demo_id": int(demo_id) if demo_id else None,
        "team_ids": _decode_team_ids_form(team_ids),
        "team_summaries": _decode_team_summaries_form(team_summaries),
        "self_contact_id": self_contact_id,
        "boss_contact_id": boss_contact_id,
        "org_id": int(org_id) if org_id else None,
    }
    if wake_reasons:
        payload["wake_reasons"] = wake_reasons
    return payload


def _ensure_subscription(
    subscriber: pubsub_v1.SubscriberClient,
    topic_path: str,
    subscription_path: str,
    filter_str: str,
    *,
    enable_message_ordering: bool = False,
    message_retention_seconds: int | None = None,
):
    """Create or update a single subscription (blocking). Idempotent."""
    expiration_policy = pubsub_v1.types.ExpirationPolicy(ttl=None)
    request = {
        "name": subscription_path,
        "topic": topic_path,
        "expiration_policy": expiration_policy,
        "filter": filter_str,
    }
    if message_retention_seconds is not None:
        request["message_retention_duration"] = duration_pb2.Duration(
            seconds=message_retention_seconds,
        )
    if enable_message_ordering:
        request["enable_message_ordering"] = True

    try:
        subscriber.create_subscription(request=request)
        return
    except Exception as e:
        if "already exists" not in str(e).lower():
            raise

    if enable_message_ordering:
        existing = subscriber.get_subscription(
            request={"subscription": subscription_path},
        )
        if not existing.enable_message_ordering:
            # enable_message_ordering cannot be changed on an existing
            # subscription — the only way to add it is delete + recreate.
            subscriber.delete_subscription(
                request={"subscription": subscription_path},
            )
            subscriber.create_subscription(request=request)
            return

    # The Pub/Sub emulator does not implement subscription field masks the
    # same way as production GCS Pub/Sub. Subscriptions are already created
    # above; skip the production-only expiration-policy refresh locally.
    if os.environ.get("PUBSUB_EMULATOR_HOST"):
        return

    subscription = pubsub_v1.types.Subscription(
        name=subscription_path,
        expiration_policy=expiration_policy,
    )
    subscriber.update_subscription(
        request={
            "update_mask": {"paths": ["expiration_policy.ttl"]},
            "subscription": subscription,
        },
    )


async def _ensure_topic_and_subscriptions(topic_name: str) -> dict[str, str]:
    """Create the assistant topic and its four subscriptions. Idempotent.

    Returns a mapping of the resource paths that were ensured. Safe to call
    repeatedly: topic creation swallows "already exists" and each
    subscription is upserted via ``_ensure_subscription``.
    """
    publisher, subscriber = await asyncio.to_thread(_get_pubsub_clients)

    topic_path = publisher.topic_path(SETTINGS.gcp_project_id, topic_name)
    subscription_path = subscriber.subscription_path(
        SETTINGS.gcp_project_id,
        f"{topic_name}-sub",
    )
    outbound_subscription_path = subscriber.subscription_path(
        SETTINGS.gcp_project_id,
        f"{topic_name}-outbound-sub",
    )
    actions_subscription_path = subscriber.subscription_path(
        SETTINGS.gcp_project_id,
        f"{topic_name}-actions-sub",
    )
    system_error_subscription_path = subscriber.subscription_path(
        SETTINGS.gcp_project_id,
        f"{topic_name}-system-error-sub",
    )

    # Create topic (idempotent)
    try:
        await asyncio.to_thread(
            publisher.create_topic,
            request={"name": topic_path},
        )
    except Exception as e:
        if "already exists" not in str(e).lower():
            raise

    # Create/update all subscriptions in parallel
    await asyncio.gather(
        asyncio.to_thread(
            _ensure_subscription,
            subscriber,
            topic_path,
            subscription_path,
            'attributes.thread = "inbound"',
        ),
        asyncio.to_thread(
            _ensure_subscription,
            subscriber,
            topic_path,
            outbound_subscription_path,
            'attributes.thread = "unify_message_outbound"',
        ),
        asyncio.to_thread(
            _ensure_subscription,
            subscriber,
            topic_path,
            actions_subscription_path,
            'attributes.thread = "action_event"',
            enable_message_ordering=True,
            message_retention_seconds=1800,
        ),
        asyncio.to_thread(
            _ensure_subscription,
            subscriber,
            topic_path,
            system_error_subscription_path,
            'attributes.thread = "system_error"',
        ),
    )

    return {
        "topic_path": topic_path,
        "subscription_path": subscription_path,
        "actions_subscription_path": actions_subscription_path,
        "system_error_subscription_path": system_error_subscription_path,
    }


async def _ensure_assistant_topic_on_wake(assistant_id: str) -> None:
    """Best-effort guard guaranteeing the assistant topic exists at wake time.

    A missing topic permanently dead-ends a wake: the ``vm_ready`` handshake
    cannot publish ``assistant_desktop_ready`` and the assistant cannot
    receive any inbound messages (its subscriptions are gone). The topic is
    only otherwise provisioned at assistant-creation time, so re-wakes of an
    assistant whose topic was deleted would never recover.

    A single ``get_topic`` keeps the steady-state cost to one RPC; the full
    create+subscriptions path only runs when the topic is actually missing.
    Failures are logged and surfaced via observability but never block the
    wake, mirroring the best-effort topic provisioning at creation time.
    """
    topic_name = SETTINGS.assistant_topic(assistant_id)
    try:
        publisher, _ = await asyncio.to_thread(_get_pubsub_clients)
        topic_path = publisher.topic_path(SETTINGS.gcp_project_id, topic_name)
        try:
            await asyncio.to_thread(
                publisher.get_topic,
                request={"topic": topic_path},
            )
            return
        except GcpNotFound:
            pass
        await _ensure_topic_and_subscriptions(topic_name)
        emit_observability_event(
            "infra.job_start.topic_recreated",
            assistant_id=assistant_id,
            topic_name=topic_name,
        )
    except Exception as e:
        logger.warning(
            "Failed to ensure pubsub topic %s on wake for assistant %s: %s",
            topic_name,
            assistant_id,
            e,
        )
        emit_observability_event(
            "infra.job_start.topic_ensure_failed",
            assistant_id=assistant_id,
            topic_name=topic_name,
            error=str(e),
        )


# create pubsub topic
@router.post("/pubsub/topic")
async def create_pubsub_topic(topic_name: str = Form(...)):
    """
    Create a Google Cloud Pub/Sub topic and subscription with the assistant_id as
    the name.
    """
    try:
        ensured = await _ensure_topic_and_subscriptions(topic_name)

        return {
            "success": True,
            "message": "Topic and subscriptions ensured with no expiration",
            "topic_name": ensured["topic_path"],
            "subscription_name": ensured["subscription_path"],
            "actions_subscription_name": ensured["actions_subscription_path"],
            "system_error_subscription_name": ensured["system_error_subscription_path"],
            "project_id": SETTINGS.gcp_project_id,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create topic and subscription: {str(e)}",
        )


# delete pubsub topic
@router.delete("/pubsub/topic")
async def delete_pubsub_topic(topic_name: str = Form(...)):
    """
    Delete a Google Cloud Pub/Sub topic with the assistant_id as the topic name.
    Subscriptions are explicitly deleted first to avoid orphaned resources.
    """

    def _delete_topic_and_subs():
        publisher, subscriber = _get_pubsub_clients()
        topic_path = publisher.topic_path(SETTINGS.gcp_project_id, topic_name)

        try:
            for subscription_name in publisher.list_topic_subscriptions(
                request={"topic": topic_path},
            ):
                try:
                    subscriber.delete_subscription(
                        request={"subscription": subscription_name},
                    )
                except GcpNotFound:
                    pass
        except GcpNotFound:
            pass

        try:
            publisher.delete_topic(request={"topic": topic_path})
        except GcpNotFound:
            pass

        return topic_path

    try:
        topic_path = await asyncio.to_thread(_delete_topic_and_subs)

        return {
            "success": True,
            "message": "Topic deleted successfully",
            "topic_name": topic_path,
            "project_id": SETTINGS.gcp_project_id,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete topic: {str(e)}")


# create kubernetes job
@router.post("/job/create")
async def create_kubernetes_job(
    namespace: str = Form(SETTINGS.default_namespace),
    image: str = Form(DEFAULT_UNITY_IMAGE),
):
    """
    Create a Kubernetes Job for a Unity assistant.

    Args:
        namespace: Kubernetes namespace (optional, defaults to production/staging)
        image: Docker image to use (optional, defaults to latest unity image)
    """
    try:
        batch_api, core_api, networking_api, _coord = await _get_k8s_clients()

        random_id = f"u{uuid.uuid4().hex[:4]}"
        timestamp_str = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        job_name = f"unity-{timestamp_str}-{random_id}{SETTINGS.env_suffix}"

        job = await asyncio.to_thread(
            create_unity_job,
            batch_api=batch_api,
            job_name=job_name,
            namespace=namespace,
            image=image,
            deploy_env=SETTINGS.deploy_env,
        )

        if job:
            return {
                "success": True,
                "message": "Kubernetes job created successfully",
                "job_name": job.metadata.name,
                "job_uid": job.metadata.uid,
                "namespace": namespace,
                "image": image,
                "creation_timestamp": (
                    job.metadata.creation_timestamp.isoformat()
                    if job.metadata.creation_timestamp
                    else None
                ),
            }
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create job for assistant: {job_name}",
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create Kubernetes job: {str(e)}",
        )


# delete kubernetes job
@router.delete("/job/delete")
async def delete_kubernetes_job(
    job_name: str = Form(...),
    namespace: str = Form(SETTINGS.default_namespace),
    resource_version: str = Form(None),
):
    """
    Delete a Kubernetes Job for a Unity assistant.

    Args:
        job_name: Name of the job (required)
        namespace: Kubernetes namespace (optional, defaults to production/staging)
        resource_version: The specific resource version of the job to delete
            (optional, provides optimistic locking)
    """
    try:
        batch_api, core_api, networking_api, _coord = await _get_k8s_clients()

        success = await asyncio.to_thread(
            delete_job,
            batch_api,
            job_name,
            namespace,
            resource_version,
        )

        if success:
            return {
                "success": True,
                "message": f"Job deleted successfully: {job_name}",
                "job_name": job_name,
                "namespace": namespace,
            }
        elif resource_version:
            raise HTTPException(
                status_code=409,
                detail=f"Job {job_name} has changed since it was last read (resource_version conflict)",
            )
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete job: {job_name}",
            )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete job: {str(e)}")


@router.patch("/job/labels")
async def patch_kubernetes_job_labels(
    job_name: str = Form(...),
    labels: str = Form(...),
    namespace: str = Form(SETTINGS.default_namespace),
):
    """
    Patch labels on an existing Kubernetes Job.

    Args:
        job_name: Name of the job (required)
        labels: JSON-encoded dict of labels to set (required)
        namespace: Kubernetes namespace (optional, defaults to production/staging)
    """
    try:
        parsed_labels = json.loads(labels)

        batch_api, core_api, networking_api, _coord = await _get_k8s_clients()

        success = await asyncio.to_thread(
            patch_job_labels,
            batch_api,
            job_name,
            parsed_labels,
            namespace,
        )

        if success:
            return {
                "success": True,
                "message": f"Job labels patched: {job_name}",
                "job_name": job_name,
                "labels": parsed_labels,
                "namespace": namespace,
            }
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Job not found: {job_name}",
            )

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Invalid JSON in labels parameter",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to patch job labels: {str(e)}",
        )


@router.get("/job/{job_name}")
async def read_job(job_name: str, namespace: str = SETTINGS.default_namespace):
    """Read a single Job's labels, annotations, and status by name.

    Used by idle containers to poll their own assignment state.
    """
    try:
        batch_api, _, _, _coord = await _get_k8s_clients()
        job = await asyncio.to_thread(
            batch_api.read_namespaced_job,
            name=job_name,
            namespace=namespace,
        )
        labels = dict(job.metadata.labels or {})
        annotations = dict(job.metadata.annotations or {})
        return {
            "success": True,
            "job_name": job.metadata.name,
            "labels": labels,
            "annotations": annotations,
            "resource_version": job.metadata.resource_version,
            "active": job.status.active or 0,
        }
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_name}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/job/start")
async def start_job(
    api_key: str = Form(...),
    medium: str = Form(...),
    assistant_id: str = Form(...),
    user_id: str = Form(...),
    user_first_name: str = Form(...),
    user_surname: str = Form(""),
    user_email: str = Form(...),
    assistant_first_name: str = Form(...),
    assistant_surname: str = Form(""),
    assistant_age: str = Form(""),
    assistant_nationality: str = Form(""),
    assistant_about: str = Form(""),
    assistant_job_title: str = Form(""),
    assistant_timezone: str = Form("UTC"),
    user_number: str = Form(""),
    assistant_number: str = Form(""),
    assistant_email: str = Form(""),
    assistant_email_provider: str = Form("google_workspace"),
    user_whatsapp_number: str = Form(""),
    assistant_whatsapp_number: str = Form(""),
    assistant_discord_bot_id: str = Form(""),
    voice_provider: str = Form(""),
    voice_id: str = Form(""),
    desktop_mode: str = Form("none"),
    desktop_url: str = Form(""),
    desktop_required: str = Form(""),
    user_desktops: str = Form(""),
    is_coordinator: str = Form("false"),
    demo_id: str = Form(""),
    team_ids: str = Form(""),
    team_summaries: str = Form(""),
    self_contact_id: int = Form(...),
    boss_contact_id: int = Form(...),
    org_id: str = Form(""),
    wake_reasons: str = Form(""),
):
    """
    Ensure an AssistantSession exists for this assistant activation.

    The session controller owns actual container and VM binding. This
    endpoint creates or updates the durable runtime intent plus the
    per-session bootstrap Secret, preserving the existing northbound
    activation contract used by adapters and Orchestra.

    Args:
        api_key: API key for authentication (required)
        medium: The type of medium (required)
        assistant_id: Unique assistant identifier (required)
        user_id: Unique user identifier (required)
        user_first_name: User's first name (required)
        user_surname: User's surname (optional, defaults to empty)
        user_email: User's email (required)
        assistant_first_name: Assistant's first name (required)
        assistant_surname: Assistant's surname (optional, defaults to empty)
        assistant_age: Assistant's age (optional, defaults to empty string)
        assistant_nationality: Assistant's nationality (optional, defaults to empty string)
        assistant_about: Assistant's about (optional, defaults to empty string)
        assistant_timezone: Assistant's timezone (required)
        user_number: User's phone number (optional, defaults to empty string)
        assistant_number: Assistant's phone number (optional, defaults to empty string)
        assistant_email: Assistant's email (optional, defaults to empty string)
        user_whatsapp_number: User's whatsapp number (optional, defaults to empty string)
        assistant_whatsapp_number: Assistant's WhatsApp pool number (optional, defaults to empty string)
        voice_provider: TTS provider (optional, defaults to empty string)
        voice_id: Voice ID (optional, defaults to empty string)
        desktop_mode: Desktop mode - ubuntu/windows/macos/none (optional, defaults to "none")
        desktop_url: URL to access the VM desktop (optional, defaults to empty string)
        user_desktops: JSON-encoded list of per-user desktop links, each
            {owner_user_id, url, os, filesys_sync} (optional, defaults to empty)
        is_coordinator: Whether this assistant is the Coordinator (optional, defaults to "false")
        demo_id: Demo assistant metadata ID (optional, empty string if not a demo)
        team_ids: JSON-encoded list of shared team IDs the assistant belongs to (optional, defaults to empty)
        team_summaries: JSON-encoded list of shared team metadata (optional, defaults to empty)
        self_contact_id: Resolved assistant-self contact ID
        boss_contact_id: Resolved boss contact ID
        org_id: Organization ID if this is an organizational assistant (optional, defaults to empty)
    """
    session_name = assistant_session_name(assistant_id)
    activation_id = None
    existing_phase = None
    reused_active_session = False
    active_session_already_running = False
    restart_in_progress = False
    idle_pool_replenish_scheduled = False
    attempted_secret_refs: list[tuple[str, str]] = []
    coord_api = None
    start_lease_name = None
    start_lease_holder_id = None
    requested_wake_reasons = _parse_wake_reasons(wake_reasons)
    causal_token = push_causal_context(
        build_causal_context(
            caller="views.job_start",
            reason="http_request",
        ),
    )

    try:
        batch_api, core_api, _, coord_api = await _get_k8s_clients()
        custom_api = await asyncio.to_thread(get_custom_objects_api)
        startup_payload = _build_startup_payload(
            api_key=api_key,
            medium=medium,
            assistant_id=assistant_id,
            user_id=user_id,
            user_first_name=user_first_name,
            user_surname=user_surname,
            user_email=user_email,
            assistant_first_name=assistant_first_name,
            assistant_surname=assistant_surname,
            assistant_age=assistant_age,
            assistant_nationality=assistant_nationality,
            assistant_about=assistant_about,
            assistant_job_title=assistant_job_title,
            assistant_timezone=assistant_timezone,
            user_number=user_number,
            assistant_number=assistant_number,
            assistant_email=assistant_email,
            assistant_email_provider=assistant_email_provider,
            user_whatsapp_number=user_whatsapp_number,
            assistant_whatsapp_number=assistant_whatsapp_number,
            assistant_discord_bot_id=assistant_discord_bot_id,
            voice_provider=voice_provider,
            voice_id=voice_id,
            desktop_mode=desktop_mode,
            desktop_url=desktop_url,
            user_desktops=user_desktops,
            is_coordinator=is_coordinator,
            demo_id=demo_id,
            team_ids=team_ids,
            team_summaries=team_summaries,
            self_contact_id=self_contact_id,
            boss_contact_id=boss_contact_id,
            org_id=org_id,
            wake_reasons=requested_wake_reasons,
        )
        (
            start_lease_name,
            start_lease_holder_id,
            start_lease_wait_ms,
        ) = await _acquire_start_job_lease(
            coord_api,
            assistant_id,
            SETTINGS.default_namespace,
        )
        if start_lease_wait_ms >= int(START_JOB_LEASE_POLL_INTERVAL_SECONDS * 1000):
            emit_observability_event(
                "infra.job_start.serialized",
                assistant_id=assistant_id,
                session_name=session_name,
                wait_ms=start_lease_wait_ms,
            )
        await _ensure_assistant_topic_on_wake(assistant_id)
        control_plane_ready, control_plane_reason = (
            await _assistant_session_control_plane_ready(
                batch_api,
                custom_api,
            )
        )
        if not control_plane_ready:
            raise HTTPException(
                status_code=503,
                detail=(
                    "AssistantSession control plane is unavailable for new "
                    f"activations ({control_plane_reason})"
                ),
            )
        existing_session = await _load_startable_assistant_session(
            custom_api,
            assistant_id,
        )
        if assistant_session_is_terminating(existing_session):
            existing_phase = (
                str(existing_session.get("status", {}).get("phase", ""))
                if existing_session
                else ""
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    "AssistantSession deletion is still in progress; retry the "
                    "wake-up shortly"
                ),
            )
        if assistant_session_stop_requested(existing_session):
            raise HTTPException(
                status_code=409,
                detail="AssistantSession stop is in progress",
            )
        existing_phase = (
            str(existing_session.get("status", {}).get("phase", ""))
            if existing_session
            else ""
        )
        existing_activation_id = (
            str(existing_session.get("spec", {}).get("activationId", ""))
            if existing_session
            else ""
        )
        observed_activation_id = (
            str(existing_session.get("status", {}).get("observedActivationId", ""))
            if existing_session
            else ""
        )
        existing_binding = session_binding(existing_session)
        release_draining = bool(
            existing_phase in {"Releasing", "Released"}
            and (
                binding_job_ref(existing_binding).get("name")
                or binding_pod_ref(existing_binding).get("name")
                or binding_vm_ref(existing_binding).get("name")
                or binding_desktop_url(existing_binding)
            ),
        )
        reused_active_session = bool(
            existing_phase in ACTIVE_PHASES and existing_activation_id,
        )
        active_session_already_running = bool(
            existing_phase == "Active" and existing_activation_id,
        )
        restart_in_progress = bool(
            release_draining
            or (
                existing_phase
                and existing_phase not in ACTIVE_PHASES
                and existing_activation_id
                and observed_activation_id
                and existing_activation_id != observed_activation_id
            ),
        )
        bootstrap_payload = _startup_payload_without_ephemeral_wake_reasons(
            startup_payload,
            keep_wake_reasons=not active_session_already_running,
        )
        activation_id = (
            existing_activation_id
            if reused_active_session or restart_in_progress
            else uuid.uuid4().hex
        )
        emit_observability_event(
            "infra.job_start.request",
            assistant_id=assistant_id,
            session_name=session_name,
            activation_id=activation_id,
            existing_phase=existing_phase,
            reused_active_session=reused_active_session,
            restart_in_progress=restart_in_progress,
            release_draining=release_draining,
            medium=medium,
            desktop_mode=desktop_mode,
        )
        secret_name = await asyncio.to_thread(
            create_or_update_bootstrap_secret,
            core_api,
            SETTINGS.default_namespace,
            assistant_id,
            activation_id,
            bootstrap_payload,
        )
        attempted_secret_refs.append((secret_name, activation_id))

        spec = build_assistant_session_spec(
            assistant_id=assistant_id,
            user_id=user_id,
            medium=medium,
            desktop_mode=desktop_mode,
            startup_secret_ref=secret_name,
            activation_id=activation_id,
            desktop_required=_parse_desktop_required_form(
                desktop_required,
                desktop_mode,
            ),
        )
        try:
            session = await asyncio.to_thread(
                create_or_update_assistant_session,
                custom_api,
                SETTINGS.default_namespace,
                assistant_id,
                spec,
            )
        except AssistantSessionTerminatingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ApiException as exc:
            if exc.status != 409 or reused_active_session or restart_in_progress:
                raise
            conflicting_session = await asyncio.to_thread(
                get_assistant_session,
                custom_api,
                SETTINGS.default_namespace,
                assistant_id,
            )
            conflicting_spec = (conflicting_session or {}).get("spec") or {}
            conflicting_status = (conflicting_session or {}).get("status") or {}
            conflicting_activation_id = str(
                conflicting_spec.get("activationId", "") or "",
            )
            if (
                conflicting_session is None
                or not conflicting_activation_id
                or conflicting_activation_id == activation_id
                or conflicting_activation_id == existing_activation_id
            ):
                raise
            emit_observability_event(
                "infra.job_start.adopted_winner",
                assistant_id=assistant_id,
                session_name=session_name,
                requested_activation_id=activation_id,
                winner_activation_id=conflicting_activation_id,
                conflicting_phase=str(conflicting_status.get("phase", "") or "")
                or None,
                conflicting_desired_state=(
                    str(conflicting_spec.get("desiredState", "") or "") or None
                ),
            )
            activation_id = conflicting_activation_id
            secret_name = await asyncio.to_thread(
                create_or_update_bootstrap_secret,
                core_api,
                SETTINGS.default_namespace,
                assistant_id,
                activation_id,
                bootstrap_payload,
            )
            attempted_secret_refs.append((secret_name, activation_id))
            spec = build_assistant_session_spec(
                assistant_id=assistant_id,
                user_id=user_id,
                medium=medium,
                desktop_mode=desktop_mode,
                startup_secret_ref=secret_name,
                activation_id=activation_id,
                desktop_required=_parse_desktop_required_form(
                    desktop_required,
                    desktop_mode,
                ),
            )
            try:
                session = await asyncio.to_thread(
                    create_or_update_assistant_session,
                    custom_api,
                    SETTINGS.default_namespace,
                    assistant_id,
                    spec,
                )
            except AssistantSessionTerminatingError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if reused_active_session:
            existing_secret_name = str(
                existing_session.get("spec", {}).get("startupSecretRef", ""),
            )
            emit_observability_event(
                "infra.job_start.reused",
                assistant_id=assistant_id,
                session_name=session_name,
                activation_id=activation_id,
                existing_phase=existing_phase,
                existing_startup_secret_ref=existing_secret_name,
                startup_secret_ref=secret_name,
            )

        activation_id = str(session.get("spec", {}).get("activationId", activation_id))
        secret_name = str(session.get("spec", {}).get("startupSecretRef", secret_name))
        await _cleanup_superseded_bootstrap_secrets(
            core_api=core_api,
            assistant_id=assistant_id,
            existing_session=existing_session,
            attempted_refs=attempted_secret_refs,
            final_secret_name=secret_name,
        )
        status = session.get("status", {})
        binding = session_binding(session)
        if not reused_active_session:
            idle_pool_replenish_scheduled = schedule_idle_job_pool_replenishment(
                extra_demand=1,
                source="views.job_start",
            )
        emit_observability_event(
            "infra.job_start.ensured",
            **assistant_session_observability_fields(
                session,
                reused_active_session=reused_active_session,
                startup_secret_ref=secret_name,
            ),
            idle_pool_replenish_scheduled=idle_pool_replenish_scheduled,
        )
        return {
            "success": True,
            "message": "AssistantSession ensured",
            "assistant_id": assistant_id,
            "session_name": session_name,
            "activation_id": activation_id,
            "phase": status.get("phase", "PendingJob"),
            "job_name": binding_job_ref(binding).get("name"),
            "reused_active_session": reused_active_session,
            "active_session_already_running": active_session_already_running,
            "wake_reasons_attached_to_startup": bool(
                requested_wake_reasons and not active_session_already_running,
            ),
        }
    except HTTPException as exc:
        emit_observability_event(
            "infra.job_start.failed",
            assistant_id=assistant_id,
            session_name=session_name,
            activation_id=activation_id,
            existing_phase=existing_phase,
            reused_active_session=reused_active_session,
            error=str(exc.detail),
            status_code=exc.status_code,
        )
        raise
    except Exception as e:
        emit_observability_event(
            "infra.job_start.failed",
            assistant_id=assistant_id,
            session_name=session_name,
            activation_id=activation_id,
            existing_phase=existing_phase,
            reused_active_session=reused_active_session,
            error=str(e),
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to ensure AssistantSession: {str(e)}",
        )
    finally:
        if start_lease_name and start_lease_holder_id and coord_api is not None:
            await asyncio.to_thread(
                release_named_lease,
                coord_api,
                start_lease_name,
                SETTINGS.default_namespace,
                start_lease_holder_id,
            )
        pop_causal_context(causal_token)


@router.get("/session/{assistant_id}")
async def read_assistant_session(assistant_id: str):
    """Read the current AssistantSession for an assistant."""
    custom_api = await asyncio.to_thread(get_custom_objects_api)
    if custom_api is None:
        raise HTTPException(
            status_code=500,
            detail="Failed to initialize AssistantSession API client",
        )
    session = await asyncio.to_thread(
        get_assistant_session,
        custom_api,
        SETTINGS.default_namespace,
        assistant_id,
    )
    if session is None:
        raise HTTPException(
            status_code=404,
            detail=f"AssistantSession not found for assistant {assistant_id}",
        )
    return session


@router.delete("/session/{assistant_id}")
async def delete_current_assistant_session(assistant_id: str):
    """Request deletion of the current AssistantSession for an assistant."""
    causal_token = push_causal_context(
        build_causal_context(
            caller="views.session_delete",
            reason="http_request",
        ),
    )
    try:
        emit_observability_event(
            "infra.session.delete.requested",
            assistant_id=assistant_id,
        )
        custom_api = await asyncio.to_thread(get_custom_objects_api)
        if custom_api is None:
            raise HTTPException(
                status_code=500,
                detail="Failed to initialize AssistantSession API client",
            )

        deleted = await asyncio.to_thread(
            delete_assistant_session,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
        )
        emit_observability_event(
            "infra.session.delete.accepted",
            assistant_id=assistant_id,
            deleted=deleted,
        )
        return {
            "success": True,
            "assistant_id": assistant_id,
            "deleted": deleted,
        }
    finally:
        pop_causal_context(causal_token)


def _persist_assistant_session_stop_request(
    custom_api: k8s_client.CustomObjectsApi,
    namespace: str,
    assistant_id: str,
    session: dict,
    *,
    source: str,
    source_reason: str,
) -> tuple[dict, str | None]:
    """Record explicit stop intent and patch the session spec to ``Stopped``."""

    binding = session_binding(session)
    current_binding_id = binding_id_from_status(binding) or None
    current_job_name = str(binding_job_ref(binding).get("name", "") or "") or None
    emit_observability_event(
        "infra.session.stop.requested",
        **assistant_session_observability_fields(
            session,
            assistant_id=assistant_id,
        ),
        release_requested_at=binding.get("releaseRequestedAt"),
        release_completed_at=binding.get("releaseCompletedAt"),
        source=source,
        source_reason=source_reason,
    )
    if current_binding_id:
        patch_assistant_session_status(
            custom_api,
            namespace,
            assistant_id,
            expected_binding_id=current_binding_id,
            suspend_intent=build_suspend_intent(
                binding_id=current_binding_id,
                intent=SUSPEND_INTENT_STOP,
                source=source,
                source_reason=source_reason,
                job_name=current_job_name,
            ),
            source=source,
        )
    updated = patch_assistant_session_spec(
        custom_api,
        namespace,
        assistant_id,
        desired_state=DESIRED_STATE_STOPPED,
    )
    emit_observability_event(
        "infra.session.stop.accepted",
        **assistant_session_observability_fields(
            updated,
            assistant_id=assistant_id,
        ),
        previous_phase=((session.get("status") or {}).get("phase") or None),
        previous_binding_id=current_binding_id,
        source=source,
        source_reason=source_reason,
    )
    return updated, current_binding_id


@router.post("/session/{assistant_id}/stop")
async def stop_current_assistant_session(assistant_id: str):
    """Declare that the assistant runtime should stop."""

    causal_token = push_causal_context(
        build_causal_context(
            caller="views.session_stop",
            reason="http_request",
        ),
    )
    try:
        custom_api = await asyncio.to_thread(get_custom_objects_api)
        if custom_api is None:
            raise HTTPException(
                status_code=500,
                detail="Failed to initialize AssistantSession API client",
            )

        session = await asyncio.to_thread(
            get_assistant_session,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
        )
        if session is None:
            return {
                "success": True,
                "assistant_id": assistant_id,
                "stopped": False,
                "reason": "not_found",
                "binding_id": None,
            }

        updated, current_binding_id = await asyncio.to_thread(
            _persist_assistant_session_stop_request,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
            session,
            source="views.session_stop",
            source_reason="api_session_stop",
        )
        return {
            "success": True,
            "assistant_id": assistant_id,
            "stopped": True,
            "desired_state": ((updated.get("spec") or {}).get("desiredState") or ""),
            "binding_id": current_binding_id,
        }
    finally:
        pop_causal_context(causal_token)


# stop kubernetes job
@router.post("/job/stop")
async def stop_job(
    job_name: str = Form(...),
    namespace: str = Form(SETTINGS.default_namespace),
):
    """
    Stop a Kubernetes Job for a Unity assistant.
    """
    causal_token = push_causal_context(
        build_causal_context(
            caller="views.job_stop",
            reason="http_request",
        ),
    )
    try:
        emit_observability_event(
            "infra.job_stop.requested",
            job_name=job_name,
            namespace=namespace,
        )
        batch_api, core_api, networking_api, _coord = await _get_k8s_clients()
        job = await asyncio.to_thread(
            batch_api.read_namespaced_job,
            name=job_name,
            namespace=namespace,
        )
        del core_api, networking_api, _coord
        labels = dict(job.metadata.labels or {})
        annotations = dict(job.metadata.annotations or {})
        session_stop_requested = False
        session_stop_assistant_id = None
        session_binding_id = (
            str(labels.get(SESSION_BINDING_ID_LABEL, "") or "")
            or str(annotations.get(BINDING_ID_ANNOTATION, "") or "")
            or None
        )
        session_name = (
            str(labels.get(SESSION_REF_LABEL, "") or "")
            or str(annotations.get(SESSION_REF_ANNOTATION, "") or "")
            or None
        )
        owner_assistant_id = str(labels.get("assistant-id", "") or "") or None
        if (
            owner_assistant_id
            and session_name == assistant_session_name(owner_assistant_id)
            and session_binding_id
        ):
            custom_api = await asyncio.to_thread(get_custom_objects_api)
            if custom_api is not None:
                session = await asyncio.to_thread(
                    get_assistant_session,
                    custom_api,
                    namespace,
                    owner_assistant_id,
                )
                current_binding = session_binding(session)
                current_binding_id = binding_id_from_status(current_binding) or None
                current_job_name = (
                    str(binding_job_ref(current_binding).get("name", "") or "") or None
                )
                if (
                    session is not None
                    and current_binding_id == session_binding_id
                    and current_job_name == job_name
                ):
                    await asyncio.to_thread(
                        _persist_assistant_session_stop_request,
                        custom_api,
                        namespace,
                        owner_assistant_id,
                        session,
                        source="views.job_stop",
                        source_reason="api_job_stop",
                    )
                    session_stop_requested = True
                    session_stop_assistant_id = owner_assistant_id
        success = await asyncio.to_thread(suspend_job, batch_api, job_name, namespace)
        if success:
            emit_observability_event(
                "infra.job_stop.accepted",
                job_name=job_name,
                namespace=namespace,
                assistant_id=session_stop_assistant_id,
                binding_id=session_binding_id,
                session_stop_requested=session_stop_requested,
            )
            return {
                "success": True,
                "message": f"Job suspended successfully: {job_name}",
                "assistant_id": session_stop_assistant_id,
                "binding_id": session_binding_id,
                "session_stop_requested": session_stop_requested,
            }
        raise HTTPException(
            status_code=500,
            detail=f"Failed to suspend job: {job_name}",
        )
    except HTTPException:
        emit_observability_event(
            "infra.job_stop.failed",
            job_name=job_name,
            namespace=namespace,
        )
        raise
    except Exception as e:
        emit_observability_event(
            "infra.job_stop.failed",
            job_name=job_name,
            namespace=namespace,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail=f"Failed to suspend job: {str(e)}")
    finally:
        pop_causal_context(causal_token)


def _job_started_at(job) -> datetime | None:
    """Best-effort UTC start time for a Unity job."""
    creation_timestamp = getattr(job.metadata, "creation_timestamp", None)
    if creation_timestamp is not None:
        if creation_timestamp.tzinfo is None:
            return creation_timestamp.replace(tzinfo=timezone.utc)
        return creation_timestamp.astimezone(timezone.utc)

    job_name = str(getattr(job.metadata, "name", "") or "")
    timestamp_str = "-".join(
        filter(
            lambda part: part.isdigit() and len(part) in [2, 4],
            job_name.split("-"),
        ),
    )
    if not timestamp_str:
        return None
    try:
        return datetime.strptime(
            timestamp_str,
            "%Y-%m-%d-%H-%M-%S",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _job_date_selector(now: datetime, hours: int | None) -> str | None:
    """Return a unity-date label selector covering the full requested window."""
    if hours is None:
        return None

    cutoff = now - timedelta(hours=hours)
    start_date = cutoff.date()
    end_date = now.date()
    day_count = (end_date - start_date).days
    relevant_dates = [
        (start_date + timedelta(days=offset)).isoformat()
        for offset in range(day_count + 1)
    ]
    return f"unity-date in ({','.join(relevant_dates)})"


def _job_status(job) -> str:
    """Translate a Kubernetes Job status into the public /infra/jobs contract."""
    if job.status.active:
        return "Running"
    if job.status.succeeded:
        return "Completed"
    if job.status.failed:
        return "Failed"
    return "Unknown"


def _serialize_job(job) -> dict:
    """Serialize a Kubernetes Job into the schema returned by /infra/jobs."""
    labels = dict(job.metadata.labels or {})
    return {
        "job_name": job.metadata.name,
        "assistant_id": labels.get("assistant-id", "unknown"),
        "labels": labels,
        "status": _job_status(job),
        "resource_version": job.metadata.resource_version,
        "creation_timestamp": (
            job.metadata.creation_timestamp.isoformat()
            if job.metadata.creation_timestamp
            else None
        ),
        "active": job.status.active or 0,
        "succeeded": job.status.succeeded or 0,
        "failed": job.status.failed or 0,
    }


# list kubernetes jobs
@router.get("/jobs")
async def list_kubernetes_jobs(
    namespace: str = SETTINGS.default_namespace,
    hours: int | None = None,
    label_selector: str = "app=unity",
):
    """
    List all Unity Kubernetes jobs in the namespace.

    Args:
        namespace: Kubernetes namespace (optional, defaults to "default")
        hours: Optional lookback window in hours. When omitted, returns all
            matching jobs without time-based filtering.
        label_selector: K8s label selector (optional, defaults to "app=unity")
    """
    try:
        batch_api, core_api, networking_api, _coord = await _get_k8s_clients()

        now = datetime.now(timezone.utc)
        date_filter = _job_date_selector(now, hours)
        full_selector = label_selector
        if date_filter:
            full_selector = (
                f"{label_selector},{date_filter}" if label_selector else date_filter
            )

        jobs = await asyncio.to_thread(
            batch_api.list_namespaced_job,
            namespace=namespace,
            label_selector=full_selector,
        )
        job_items = list(jobs.items)
        if hours is not None:
            cutoff = now - timedelta(hours=hours)
            job_items = [
                job
                for job in job_items
                if (started_at := _job_started_at(job)) is None or started_at >= cutoff
            ]

        job_list = [_serialize_job(job) for job in job_items]

        return {
            "success": True,
            "jobs": job_list,
            "namespace": namespace,
            "total_jobs": len(job_list),
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list jobs: {str(e)}")


# get job logs
@router.get("/job/logs")
async def get_job_logs_endpoint(
    job_name: str,
    namespace: str = SETTINGS.default_namespace,
    tail_lines: int = 10,
):
    """
    Get logs from a Kubernetes Job.

    Args:
        job_name: Name of the job (required)
        namespace: Kubernetes namespace (optional, defaults to "default")
        tail_lines: Number of lines to tail (optional, defaults to 10)
    """
    try:
        batch_api, core_api, networking_api, _coord = await _get_k8s_clients()

        result = await asyncio.to_thread(
            get_job_logs,
            core_api=core_api,
            job_name=job_name,
            namespace=namespace,
            tail_lines=tail_lines,
        )

        if result["success"]:
            return result
        else:
            raise HTTPException(status_code=404, detail=result["message"])

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get job logs: {str(e)}")


# get latest unity image commit hash
@router.get("/image")
async def get_latest_unity_image_commit():
    """
    Get the commit hash of the latest Unity Docker image from a text file in Google Cloud Storage.

    Returns:
        JSON response with image details with commit hash
    """
    try:
        storage_client = storage.Client(credentials=_service_account_credentials())

        # Define the bucket and file path
        bucket_name = "unity-image-hash"
        blob_name = SETTINGS.image_hash_blob

        try:
            # Get the bucket
            bucket = storage_client.bucket(bucket_name)

            # Get the blob (file)
            blob = bucket.blob(blob_name)

            # Check if the file exists
            if not blob.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"File {blob_name} not found in bucket {bucket_name}",
                )

            # Download and read the content
            content = blob.download_as_text()
            commit_hash = content.strip()

            # Get blob metadata
            blob.reload()

            return {"commit_hash": commit_hash}

        except HTTPException:
            raise
        except Exception as gcs_error:
            print(f"❌ Error accessing GCS: {gcs_error}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to read image hash from GCS: {str(gcs_error)}",
            )

    except HTTPException:
        raise
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get latest Unity image commit: {str(e)}",
        )


# =============================================================================
# Tunnel Management Endpoints (user API key auth, not admin key)
# =============================================================================

tunnel_router = APIRouter()


@tunnel_router.post("/tunnel/register", response_model=TunnelRegisterResponse)
async def register_tunnel_endpoint(
    request_body: TunnelRegisterRequest,
    request: Request,
):
    """
    Register a new tunnel to expose a user's local application via a public URL.

    Authenticated via user API key (Authorization: Bearer <key>).
    The user_id is derived from the API key via Orchestra.

    Args:
        local_port: The port on the user's machine to expose (default 8080).
        name: Optional friendly name for the tunnel.
    """
    api_key = extract_api_key(request)
    user_info = await authenticate_user_api_key(api_key)
    user_id = str(user_info["user_id"])

    result = register_tunnel(
        user_id=user_id,
        local_port=request_body.local_port,
        name=request_body.name,
        protocol=request_body.protocol,
    )
    return TunnelRegisterResponse(**result)


@tunnel_router.get("/tunnel/{tunnel_id}", response_model=TunnelStatusResponse)
async def get_tunnel_status_endpoint(tunnel_id: str, request: Request):
    """
    Get the current status of a tunnel.

    Authenticated via user API key. Only the tunnel owner can query status.
    """
    api_key = extract_api_key(request)
    user_info = await authenticate_user_api_key(api_key)
    user_id = str(user_info["user_id"])

    result = get_tunnel_status(tunnel_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Tunnel not found: {tunnel_id}")

    if result.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail=f"Tunnel not found: {tunnel_id}")

    return TunnelStatusResponse(**result)


@tunnel_router.delete("/tunnel/{tunnel_id}", response_model=TunnelDeleteResponse)
async def delete_tunnel_endpoint(tunnel_id: str, request: Request):
    """
    Delete a tunnel and disconnect the client.

    Authenticated via user API key. The user_id for ownership check is
    derived from the API key.
    """
    api_key = extract_api_key(request)
    user_info = await authenticate_user_api_key(api_key)
    user_id = str(user_info["user_id"])

    result = unregister_tunnel(tunnel_id=tunnel_id, user_id=user_id)
    if not result["deleted"]:
        status_code = 404 if "not found" in result["message"].lower() else 403
        raise HTTPException(status_code=status_code, detail=result["message"])
    return TunnelDeleteResponse(**result)


@tunnel_router.get("/tunnels", response_model=TunnelListResponse)
async def list_tunnels_endpoint(request: Request):
    """
    List all tunnels owned by the authenticated user.

    Authenticated via user API key. The user_id is derived from the API key.
    """
    api_key = extract_api_key(request)
    user_info = await authenticate_user_api_key(api_key)
    user_id = str(user_info["user_id"])

    result = list_user_tunnels(user_id)
    return TunnelListResponse(**result)


# =============================================================================
# VM Ready Notification (user API key auth)
# =============================================================================


@tunnel_router.post("/vm/ready")
async def vm_ready_endpoint(
    request_body: VMReadyRequest,
    request: Request,
):
    """Publish an assistant_desktop_ready system event when a VM finishes startup.

    Called by the VM startup script once services are running.
    Authenticated via user API key (Authorization: Bearer <unify-key>).
    """
    api_key = extract_api_key(request)
    await authenticate_user_api_key(api_key)

    assistant_id = request_body.assistant_id
    requested_binding_id = request_body.binding_id
    vm_type = request_body.vm_type
    requested_hostname = request_body.hostname or get_dns_hostname(assistant_id)
    causal_token = push_causal_context(
        build_causal_context(
            caller="views.vm_ready",
            reason="http_request",
        ),
    )
    try:
        emit_observability_event(
            "infra.vm_ready.received",
            assistant_id=assistant_id,
            requested_binding_id=requested_binding_id,
            requested_hostname=requested_hostname,
            vm_type=vm_type,
        )
        custom_api = await asyncio.to_thread(get_custom_objects_api)
        if custom_api is None:
            raise HTTPException(
                status_code=500,
                detail="Failed to initialize AssistantSession API client",
            )
        _, core_api, _, _ = await _get_k8s_clients()
        session = await asyncio.to_thread(
            get_assistant_session,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
        )
        if session is None:
            emit_observability_event(
                "infra.vm_ready.rejected",
                assistant_id=assistant_id,
                requested_binding_id=requested_binding_id,
                requested_hostname=requested_hostname,
                reason="session_missing",
                vm_type=vm_type,
            )
            raise HTTPException(
                status_code=409,
                detail="VM ready signal rejected: AssistantSession not found",
            )

        secret_name = session.get("spec", {}).get("startupSecretRef", "")
        startup_payload = await asyncio.to_thread(
            read_bootstrap_secret,
            core_api,
            SETTINGS.default_namespace,
            secret_name,
        )
        expected_api_key = str(startup_payload.get("api_key", ""))
        if expected_api_key and api_key != expected_api_key:
            emit_observability_event(
                "infra.vm_ready.rejected",
                **assistant_session_observability_fields(
                    session,
                    requested_hostname=requested_hostname,
                    reason="api_key_mismatch",
                    vm_type=vm_type,
                ),
            )
            raise HTTPException(
                status_code=401,
                detail="Ready signal API key does not match the active AssistantSession",
            )

        # ── Fresh read: all readiness decisions use the latest session state ──
        fresh = await asyncio.to_thread(
            get_assistant_session,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
        )
        if fresh is None:
            emit_observability_event(
                "infra.vm_ready.rejected",
                assistant_id=assistant_id,
                requested_binding_id=requested_binding_id,
                requested_hostname=requested_hostname,
                reason="session_disappeared",
                vm_type=vm_type,
            )
            raise HTTPException(status_code=409, detail="AssistantSession disappeared")
        fresh_spec = fresh.get("spec", {})
        fresh_status = fresh.get("status", {})
        fresh_binding = session_binding(fresh)
        current_binding_id = binding_id_from_status(fresh_binding)

        if current_binding_id != requested_binding_id:
            emit_observability_event(
                "infra.vm_ready.rejected",
                **assistant_session_observability_fields(
                    fresh,
                    requested_binding_id=requested_binding_id,
                    current_binding_id=current_binding_id or None,
                    requested_hostname=requested_hostname,
                    reason="binding_changed",
                    vm_type=vm_type,
                ),
            )
            raise HTTPException(
                status_code=409,
                detail="Ready signal came from a stale binding",
            )

        activation_id = fresh_spec.get("activationId", "")
        observed_id = fresh_status.get("observedActivationId", "")
        if activation_id and observed_id != activation_id:
            emit_observability_event(
                "infra.vm_ready.rejected",
                **assistant_session_observability_fields(
                    fresh,
                    requested_hostname=requested_hostname,
                    reason="activation_rollover",
                    vm_type=vm_type,
                    activation_id=activation_id,
                    observed_activation_id=observed_id,
                ),
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    "VM ready signal rejected: activation rollover in progress "
                    f"(spec={activation_id}, observed={observed_id})"
                ),
            )

        if assistant_session_desired_state(fresh) == DESIRED_STATE_STOPPED:
            emit_observability_event(
                "infra.vm_ready.ignored",
                **assistant_session_observability_fields(
                    fresh,
                    requested_binding_id=requested_binding_id,
                    current_binding_id=current_binding_id or None,
                    requested_hostname=requested_hostname,
                    reason="release_in_progress",
                    vm_type=vm_type,
                    release_requested_at=fresh_binding.get("releaseRequestedAt"),
                ),
            )
            return {
                "success": True,
                "accepted": False,
                "reason": "release_in_progress",
            }

        fresh_conditions = fresh_status.get("conditions", [])
        container_ready = any(
            c.get("type") == "ContainerReady" and c.get("status") == "True"
            for c in fresh_conditions
        )
        if not container_ready:
            emit_observability_event(
                "infra.vm_ready.rejected",
                **assistant_session_observability_fields(
                    fresh,
                    requested_hostname=requested_hostname,
                    reason="container_not_ready",
                    vm_type=vm_type,
                ),
            )
            raise HTTPException(
                status_code=409,
                detail="VM ready signal rejected: container is not yet ready",
            )

        existing_vm_ref = binding_vm_ref(fresh_binding)
        existing_vm_name = existing_vm_ref.get("name", "")
        if not existing_vm_name:
            emit_observability_event(
                "infra.vm_ready.rejected",
                **assistant_session_observability_fields(
                    fresh,
                    requested_binding_id=requested_binding_id,
                    current_binding_id=current_binding_id or None,
                    requested_hostname=requested_hostname,
                    reason="vm_not_yet_bound",
                    vm_type=vm_type,
                ),
            )
            raise HTTPException(
                status_code=409,
                detail="Ready signal arrived before the current binding owned a VM",
            )

        assigned_vm_ref = await asyncio.to_thread(
            verify_vm_assignment,
            existing_vm_name,
            current_binding_id,
            assistant_id,
        )

        if assigned_vm_ref is None:
            emit_observability_event(
                "infra.vm_ready.rejected",
                **assistant_session_observability_fields(
                    fresh,
                    requested_hostname=requested_hostname,
                    reason="no_assigned_vm",
                    vm_type=vm_type,
                ),
            )
            logger.warning(
                "Ignoring VM ready from %s for assistant %s: binding no longer owns a VM",
                requested_hostname,
                assistant_id,
            )
            raise HTTPException(
                status_code=409,
                detail="Ready signal arrived after the current binding lost VM ownership",
            )
        if not vm_refs_match({"hostname": requested_hostname}, assigned_vm_ref):
            emit_observability_event(
                "infra.vm_ready.rejected",
                **assistant_session_observability_fields(
                    fresh,
                    requested_binding_id=requested_binding_id,
                    requested_hostname=requested_hostname,
                    assigned_hostname=assigned_vm_ref.get("hostname"),
                    assigned_vm_name=assigned_vm_ref.get("name"),
                    reason="stale_vm_ready",
                    vm_type=vm_type,
                ),
            )
            logger.warning(
                "Ignoring stale VM ready from %s for assistant %s; current assigned VM is %s",
                requested_hostname,
                assistant_id,
                assigned_vm_ref.get("hostname"),
            )
            raise HTTPException(
                status_code=409,
                detail="Ready signal came from a VM that is no longer assigned to the current binding",
            )
        hostname = str(assigned_vm_ref.get("hostname", requested_hostname))

        ready = await asyncio.to_thread(
            probe_vm_agent_service_authenticated,
            hostname,
            expected_api_key or api_key,
        )
        if not ready:
            emit_observability_event(
                "infra.vm_ready.rejected",
                **assistant_session_observability_fields(
                    fresh,
                    requested_hostname=requested_hostname,
                    assigned_hostname=hostname,
                    assigned_vm_name=assigned_vm_ref.get("name"),
                    reason="authenticated_probe_failed",
                    vm_type=vm_type,
                ),
            )
            logger.warning(
                "Authenticated VM readiness probe failed for %s (assistant %s)",
                hostname,
                assistant_id,
            )
            raise HTTPException(
                status_code=503,
                detail=f"VM agent not ready at {hostname}",
            )

        message_id = await _publish_desktop_ready(
            assistant_id,
            hostname,
            vm_type,
            binding_id=current_binding_id,
        )
        await asyncio.to_thread(
            record_assistant_session_signal,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
            signal_name=SIGNAL_DESKTOP_READY,
            payload=build_binding_signal(
                binding_id=current_binding_id,
                state="ready",
                hostname=hostname,
                desktopUrl=f"https://{hostname}",
                messageId=message_id,
            ),
            source="views.vm_ready",
        )
        emit_observability_event(
            "infra.vm_ready.accepted",
            **assistant_session_observability_fields(
                fresh,
                binding_id=current_binding_id,
                assigned_hostname=hostname,
                assigned_vm_name=assigned_vm_ref.get("name"),
                requested_hostname=requested_hostname,
                vm_type=vm_type,
                message_id=message_id,
            ),
        )

        return {
            "success": True,
            "message_id": message_id,
            "assistant_id": assistant_id,
            "mode": "session",
        }
    finally:
        pop_causal_context(causal_token)


# =============================================================================
# VM Pool Endpoints
# =============================================================================


@router.post("/vm/pool/provision")
async def provision_pool_endpoint(request: PoolProvisionRequest):
    """Create new idle pool VMs.

    Provisions `count` VMs of the specified type with generic names,
    static IPs, DNS records, and idle labels.
    """
    results = []

    # Find next available pool number(s)
    existing = await asyncio.to_thread(list_pool_vms, request.vm_type)
    existing_names = {vm["vm_name"] for vm in existing}

    n = 1
    provisioned = 0
    consecutive_failures = 0
    max_failures = 3
    while provisioned < request.count:
        from .vm_config import pool_vm_name_prefix

        candidate = (
            f"{pool_vm_name_prefix(request.vm_type)}-{request.vm_type}-{n}"
            f"{SETTINGS.env_suffix}"
        )
        if candidate not in existing_names:
            try:
                result = await asyncio.to_thread(provision_pool_vm, request.vm_type, n)
                results.append(result)
                provisioned += 1
                consecutive_failures = 0
            except Exception as e:
                logger.error(f"Failed to provision pool VM #{n}: {e}")
                results.append({"error": str(e), "n": n})
                consecutive_failures += 1
                if consecutive_failures >= max_failures:
                    logger.error(
                        f"Aborting pool provisioning after {max_failures} "
                        f"consecutive failures ({provisioned}/{request.count} provisioned)",
                    )
                    break
        n += 1

    return {"provisioned": results}


@router.post("/vm/pool/start")
async def start_pool_vm_endpoint(request: PoolStartVMRequest):
    """Start a specific stopped pool VM by type and number.

    For manual testing — mimics what replenish does for a single VM.
    Injects github-token and starts the VM; the startup script handles the rest.
    """
    try:
        result = await asyncio.to_thread(
            start_pool_vm,
            request.vm_type,
            request.vm_number,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Conflict as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/vm/pool/assign", response_model=PoolAssignResponse)
async def assign_pool_endpoint(request: PoolAssignRequest):
    """Assign a pool VM to an assistant.

    Releases any existing assignment first, then claims a fresh idle VM
    (race-safe via label CAS), creates/attaches the assistant's persistent
    disk, generates SSH keys, and updates metadata to trigger the on-VM
    watcher.
    """
    try:
        result = await asyncio.get_running_loop().run_in_executor(
            ASSIGN_EXECUTOR,
            partial(
                assign_pool_vm,
                assistant_id=request.assistant_id,
                binding_id=request.binding_id,
                unify_apikey=request.unify_apikey,
                vm_type=request.vm_type,
                vm_number=request.vm_number,
            ),
        )

        asyncio.get_running_loop().run_in_executor(
            POOL_MAINTENANCE_EXECUTOR,
            partial(replenish_pool, request.vm_type, extra_demand=1),
        )

        return PoolAssignResponse(**result)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to assign pool VM: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _resolve_release_vm_name(
    request: PoolReleaseRequest,
) -> tuple[str | None, dict[str, object] | None]:
    """Resolve a job-targeted release request to the exact VM name, if any."""

    if not request.binding_id:
        raise HTTPException(
            status_code=400,
            detail="binding_id is required for binding-scoped VM release",
        )

    if request.vm_name and request.job_name:
        raise HTTPException(
            status_code=400,
            detail="Provide at most one of vm_name or job_name",
        )

    if request.vm_name:
        return request.vm_name, None

    if not request.job_name:
        raise HTTPException(
            status_code=400,
            detail="Provide either vm_name or job_name for VM release",
        )

    custom_api = await asyncio.to_thread(get_custom_objects_api)
    if custom_api is None:
        raise HTTPException(
            status_code=500,
            detail="Failed to initialize AssistantSession API client",
        )

    session = await asyncio.to_thread(
        get_assistant_session,
        custom_api,
        SETTINGS.default_namespace,
        request.assistant_id,
    )
    if session is None:
        emit_observability_event(
            "infra.vm_release.skipped",
            assistant_id=request.assistant_id,
            job_name=request.job_name,
            reason="session_missing",
        )
        return None, {
            "released": False,
            "assistant_id": request.assistant_id,
            "binding_id": request.binding_id,
            "job_name": request.job_name,
            "stale": True,
            "message": "No current AssistantSession for job-targeted release",
        }

    binding = session_binding(session)
    current_binding_id = binding_id_from_status(binding)
    if current_binding_id != request.binding_id:
        emit_observability_event(
            "infra.vm_release.skipped",
            assistant_id=request.assistant_id,
            binding_id=request.binding_id,
            current_binding_id=current_binding_id or None,
            job_name=request.job_name,
            reason="binding_changed",
        )
        return None, {
            "released": False,
            "assistant_id": request.assistant_id,
            "binding_id": request.binding_id,
            "current_binding_id": current_binding_id or None,
            "job_name": request.job_name,
            "stale": True,
            "message": "Binding no longer owns the current session VM",
        }

    current_job_name = str(binding_job_ref(binding).get("name", "") or "")
    if current_job_name != request.job_name:
        emit_observability_event(
            "infra.vm_release.skipped",
            assistant_id=request.assistant_id,
            binding_id=request.binding_id,
            job_name=request.job_name,
            current_job_name=current_job_name,
            reason="job_changed",
        )
        return None, {
            "released": False,
            "assistant_id": request.assistant_id,
            "binding_id": request.binding_id,
            "job_name": request.job_name,
            "current_job_name": current_job_name or None,
            "stale": True,
            "message": "Job no longer owns the current session VM",
        }

    resolved_vm_ref = resolve_current_binding_vm_ref(
        binding,
    )
    vm_name = str(resolved_vm_ref.get("name", "") or "")
    owned_runtime_vms: list[dict[str, object]] = []
    disk_vm_name: str | None = None
    if not vm_name:
        (owned_runtime_vms, _), disk_vm_name = await asyncio.gather(
            asyncio.to_thread(
                split_binding_runtime_vms,
                request.assistant_id,
                binding_id=request.binding_id,
            ),
            asyncio.to_thread(find_vm_with_disk, request.assistant_id),
        )
        resolved_vm_ref = resolve_current_binding_vm_ref(
            binding,
            owned_runtime_vms=owned_runtime_vms,
            disk_vm_name=disk_vm_name,
        )
        vm_name = str(resolved_vm_ref.get("name", "") or "")
    if not vm_name:
        emit_observability_event(
            "infra.vm_release.skipped",
            **assistant_session_observability_fields(
                session,
                assistant_id=request.assistant_id,
            ),
            requested_binding_id=request.binding_id,
            requested_job_name=request.job_name,
            reason="no_vm_ref",
            release_requested_at=binding.get("releaseRequestedAt"),
            release_completed_at=binding.get("releaseCompletedAt"),
            owned_runtime_vm_names=[
                str(vm.get("vm_name", "") or "")
                for vm in owned_runtime_vms
                if vm.get("vm_name")
            ],
            disk_vm_name=disk_vm_name,
        )
        return None, {
            "released": False,
            "assistant_id": request.assistant_id,
            "binding_id": request.binding_id,
            "job_name": request.job_name,
            "message": "Current session has no VM to release",
        }

    return vm_name, None


@router.post("/vm/pool/release")
async def release_pool_endpoint(request: PoolReleaseRequest):
    """Start guest cleanup for a pool VM release.

    The VM transitions to ``releasing`` immediately so it is no longer
    claimable. The guest watcher later calls back into Comms to detach the
    assistant disk and mark the VM idle once cleanup is actually complete.
    VMs on an outdated guest contract are retired and replenished instead of
    being returned to idle. Product callers should pass ``job_name`` or
    ``vm_name`` so Comms can verify that the cleanup request still owns the
    current runtime before releasing anything.
    """
    try:
        resolved_vm_name, skipped = await _resolve_release_vm_name(request)
        if skipped is not None:
            return skipped

        emit_observability_event(
            "infra.vm_release.requested",
            assistant_id=request.assistant_id,
            binding_id=request.binding_id,
            job_name=request.job_name,
            requested_vm_name=request.vm_name,
            resolved_vm_name=resolved_vm_name,
        )
        result = await asyncio.to_thread(
            release_pool_vm,
            request.assistant_id,
            request.binding_id,
            vm_name=resolved_vm_name,
            release_generation=request.release_generation,
        )
        if result.get("retired"):
            asyncio.get_running_loop().run_in_executor(
                POOL_MAINTENANCE_EXECUTOR,
                partial(replenish_pool, result.get("vm_type", "ubuntu")),
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to release pool VM: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/vm/pool/disk/{assistant_id}")
async def delete_pool_disk_endpoint(assistant_id: str):
    """Delete an assistant's persistent disk (on unhire)."""
    try:
        deleted = await asyncio.to_thread(delete_assistant_disk, assistant_id)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"No disk found for assistant {assistant_id}",
            )
        return {"assistant_id": assistant_id, "deleted": deleted}
    except AssistantDiskInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete assistant disk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/vm/pool/disk/detach/{assistant_id}")
async def detach_pool_disk_endpoint(assistant_id: str):
    """Detach an assistant's persistent disk from its assigned VM.

    Finds the VM currently assigned to the assistant by label lookup,
    then detaches the disk. The disk is preserved for re-attachment.
    """
    try:
        vms = await asyncio.to_thread(list_pool_vms)
        sanitized = assistant_id.lower().replace("_", "-")
        assigned = [
            vm
            for vm in vms
            if vm["pool_role"] == "assigned" and vm.get("assistant_id") == sanitized
        ]
        if assigned:
            vm_name = assigned[0]["vm_name"]
        else:
            vm_name = await asyncio.to_thread(find_vm_with_disk, assistant_id)
            if not vm_name:
                raise HTTPException(
                    status_code=404,
                    detail=f"No VM found with disk for assistant {assistant_id}",
                )
        detached = await asyncio.to_thread(detach_assistant_disk, vm_name, assistant_id)
        return {
            "assistant_id": assistant_id,
            "vm_name": vm_name,
            "detached": detached,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to detach assistant disk: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _parse_utc_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO8601 timestamp into a timezone-aware UTC datetime."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _runtime_state_empty(runtime_state: dict[str, object]) -> bool:
    """Return whether an assistant currently owns no runtime artifacts."""

    return (
        not runtime_state["current_binding_id"]
        and not runtime_state["active_job_names"]
        and not runtime_state["owned_vms"]
        and not runtime_state["other_owned_vms"]
        and runtime_state["disk_vm_name"] is None
    )


def _terminal_session_ghost_transition_at(
    assistant_session: dict | None,
) -> datetime | None:
    """Return the best-effort timestamp for the current ghost-shaped state."""

    status = (assistant_session or {}).get("status") or {}
    candidates = [
        _parse_utc_timestamp(((status.get("suspendIntent") or {}).get("requestedAt"))),
        _parse_utc_timestamp(
            str(
                ((assistant_session or {}).get("metadata") or {}).get(
                    "creationTimestamp",
                ),
            ),
        ),
    ]
    for condition in status.get("conditions") or []:
        if not isinstance(condition, dict):
            continue
        candidates.append(_parse_utc_timestamp(condition.get("lastTransitionTime")))
    observed = [candidate for candidate in candidates if candidate is not None]
    return max(observed) if observed else None


def _terminal_session_ghost_heal_skip_reason(
    assistant_session: dict | None,
    *,
    now_utc: datetime,
) -> str | None:
    """Return why a session is not yet safe for ghost healing."""

    if assistant_session is None:
        return "already_deleted"
    if assistant_session_is_terminating(assistant_session):
        return "already_terminating"
    if assistant_session_desired_state(assistant_session) != "Running":
        return "desired_state_not_running"
    status = assistant_session.get("status") or {}
    phase = str(status.get("phase", "") or "")
    if phase != "Failed":
        return "phase_not_failed"
    if binding_id_from_status(session_binding(assistant_session)):
        return "binding_present"
    spec = assistant_session.get("spec") or {}
    if str(status.get("observedActivationId", "") or "") != str(
        spec.get("activationId", "") or "",
    ):
        return "activation_changed"
    transition_at = _terminal_session_ghost_transition_at(assistant_session)
    if transition_at is None:
        return "missing_transition_timestamp"
    if transition_at > now_utc - timedelta(
        minutes=TERMINAL_SESSION_GHOST_HEAL_GRACE_MINUTES,
    ):
        return "within_grace"
    return None


def _default_terminal_session_prune_retention_hours() -> float:
    """Return the environment-specific retention window for terminal sessions."""
    return TERMINAL_SESSION_PRUNE_DEFAULT_RETENTION_HOURS


async def _runtime_resource_state(
    assistant_id: str,
    *,
    batch_api,
    assistant_session: dict | None,
) -> dict[str, object]:
    """Return the live runtime resources currently owned by an assistant.

    Offline task runners (``app=unity-offline``) run outside the AssistantSession
    lifecycle but still hold writes against the owning body. They are reported
    alongside online Jobs so callers that gate on quiescence — most notably the
    membership-change runtime barrier behind ``/infra/runtime`` — wait for
    in-flight offline runs to drain before declaring cleanup complete.
    """

    sanitized = assistant_id.lower().replace("_", "-")
    jobs, offline_jobs = await asyncio.gather(
        asyncio.to_thread(
            batch_api.list_namespaced_job,
            namespace=SETTINGS.default_namespace,
            label_selector=f"app=unity,assistant-id={sanitized}",
        ),
        asyncio.to_thread(
            batch_api.list_namespaced_job,
            namespace=SETTINGS.default_namespace,
            label_selector=f"app=unity-offline,assistant-id={sanitized}",
        ),
    )
    active_job_names = [
        job.metadata.name
        for job in jobs.items
        if job.status.active
        and job.status.active > 0
        and not job.metadata.deletion_timestamp
    ]
    active_offline_job_names = [
        job.metadata.name
        for job in offline_jobs.items
        if job.status.active
        and job.status.active > 0
        and not job.metadata.deletion_timestamp
    ]
    current_binding_id = binding_id_from_status(session_binding(assistant_session))
    owned_vms, other_owned_vms = await asyncio.to_thread(
        split_binding_runtime_vms,
        assistant_id,
        binding_id=current_binding_id or None,
    )
    disk_vm_name = await asyncio.to_thread(find_vm_with_disk, assistant_id)
    return {
        "active_job_names": active_job_names,
        "active_offline_job_names": active_offline_job_names,
        "owned_vms": owned_vms,
        "other_owned_vms": other_owned_vms,
        "disk_vm_name": disk_vm_name,
        "current_binding_id": current_binding_id,
    }


async def _binding_runtime_resource_state(
    assistant_id: str,
    *,
    binding_id: str,
    batch_api,
) -> dict[str, object]:
    """Return the live runtime resources still owned by one binding."""

    sanitized_assistant_id = assistant_id.lower().replace("_", "-")
    sanitized_binding_id = binding_id.lower().replace("_", "-")
    jobs = await asyncio.to_thread(
        batch_api.list_namespaced_job,
        namespace=SETTINGS.default_namespace,
        label_selector=(
            f"app=unity,assistant-id={sanitized_assistant_id},"
            f"{SESSION_BINDING_ID_LABEL}={sanitized_binding_id}"
        ),
    )
    active_job_names = [
        job.metadata.name
        for job in jobs.items
        if job.status.active
        and job.status.active > 0
        and not job.metadata.deletion_timestamp
    ]
    owned_vms, _ = await asyncio.to_thread(
        split_binding_runtime_vms,
        assistant_id,
        binding_id=binding_id,
    )
    return {
        "active_job_names": active_job_names,
        "owned_vms": owned_vms,
    }


def _binding_release_status(
    assistant_session: dict | None,
    binding_id: str,
) -> dict[str, object]:
    """Return the persisted release-completion state for one binding."""

    binding = session_binding(assistant_session)
    current_binding_id = binding_id_from_status(binding)
    if current_binding_id == binding_id:
        return {
            "binding_release_recorded": bool(binding.get("releaseCompletedAt")),
            "binding_release_requested_at": binding.get("releaseRequestedAt") or None,
            "binding_release_completed_at": binding.get("releaseCompletedAt") or None,
        }
    recorded_release = released_binding(assistant_session, binding_id)
    return {
        "binding_release_recorded": bool(recorded_release),
        "binding_release_requested_at": (
            recorded_release.get("releaseRequestedAt") or None
        ),
        "binding_release_completed_at": (
            recorded_release.get("releaseCompletedAt") or None
        ),
    }


def _terminal_session_prune_ready(
    assistant_session: dict | None,
    runtime_state: dict[str, object],
) -> bool:
    """Return whether a terminal session can be safely deleted."""

    phase = str(((assistant_session or {}).get("status") or {}).get("phase", "") or "")
    return phase in TERMINAL_PHASES and _runtime_state_empty(runtime_state)


def _terminal_session_prune_recheck_skip_reason(
    assistant_session: dict | None,
) -> str | None:
    """Return why a prune candidate is no longer safe to delete."""

    if assistant_session is None:
        return "already_deleted"
    if assistant_session_is_terminating(assistant_session):
        return "already_terminating"
    if assistant_session_desired_state(assistant_session) != DESIRED_STATE_STOPPED:
        return "desired_state_running"
    phase = str(((assistant_session.get("status") or {}).get("phase", "") or ""))
    if phase not in TERMINAL_PHASES:
        return "no_longer_terminal"
    return None


@router.post("/sessions/prune-terminal")
async def prune_terminal_assistant_sessions(
    retention_hours: float | None = Query(default=None, gt=0),
    limit: int = Query(
        default=TERMINAL_SESSION_PRUNE_DEFAULT_LIMIT,
        ge=1,
        le=TERMINAL_SESSION_PRUNE_MAX_LIMIT,
    ),
):
    """Delete old terminal sessions whose runtime resources are already gone.

    This is a bounded maintenance safety net for ``Released`` / ``Failed``
    AssistantSession CRs that are no longer needed but were left behind by
    stop-only or incomplete delete flows.
    """

    batch_api, _, _, _ = await _get_k8s_clients()
    custom_api = await asyncio.to_thread(get_custom_objects_api)
    if custom_api is None:
        raise HTTPException(
            status_code=500,
            detail="Failed to initialize AssistantSession API client",
        )

    effective_retention_hours = (
        retention_hours or _default_terminal_session_prune_retention_hours()
    )
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=effective_retention_hours)
    listed = await asyncio.to_thread(
        custom_api.list_namespaced_custom_object,
        group=SETTINGS.assistant_session_group,
        version=SETTINGS.assistant_session_version,
        namespace=SETTINGS.default_namespace,
        plural=SETTINGS.assistant_session_plural,
    )
    sessions = list((listed or {}).get("items") or [])
    skip_reasons: Counter[str] = Counter()
    terminal_sessions_found = 0
    prune_candidates: list[tuple[datetime, str, dict]] = []
    ghost_healed_assistant_ids: list[str] = []
    runtime_state_cache: dict[str, dict[str, object]] = {}

    for session in sessions:
        phase = str(((session.get("status") or {}).get("phase", "") or ""))
        if phase not in TERMINAL_PHASES:
            continue
        terminal_sessions_found += 1

        metadata = session.get("metadata") or {}
        if metadata.get("deletionTimestamp"):
            skip_reasons["already_terminating"] += 1
            continue

        assistant_id = str(((session.get("spec") or {}).get("assistantId")) or "")
        if not assistant_id:
            skip_reasons["missing_assistant_id"] += 1
            continue

        ghost_heal_skip_reason = _terminal_session_ghost_heal_skip_reason(
            session,
            now_utc=now_utc,
        )
        if ghost_heal_skip_reason is None:
            runtime_state = runtime_state_cache.get(assistant_id)
            if runtime_state is None:
                runtime_state = await _runtime_resource_state(
                    assistant_id,
                    batch_api=batch_api,
                    assistant_session=session,
                )
                runtime_state_cache[assistant_id] = runtime_state
            if _runtime_state_empty(runtime_state):
                session = await asyncio.to_thread(
                    patch_assistant_session_spec,
                    custom_api,
                    SETTINGS.default_namespace,
                    assistant_id,
                    desired_state=DESIRED_STATE_STOPPED,
                )
                ghost_healed_assistant_ids.append(assistant_id)
                transition_at = _terminal_session_ghost_transition_at(session)
                ghost_age_minutes = None
                if transition_at is not None:
                    ghost_age_minutes = round(
                        (now_utc - transition_at).total_seconds() / 60,
                        2,
                    )
                emit_observability_event(
                    "infra.session.ghost_healed",
                    **assistant_session_observability_fields(
                        session,
                        assistant_id=assistant_id,
                    ),
                    ghost_age_minutes=ghost_age_minutes,
                )
            else:
                skip_reasons["ghost_runtime_resources_present"] += 1

        created_at = _parse_utc_timestamp(metadata.get("creationTimestamp"))
        if created_at is None:
            skip_reasons["missing_creation_timestamp"] += 1
            continue
        if created_at > cutoff:
            skip_reasons["within_retention"] += 1
            continue

        prune_candidates.append((created_at, assistant_id, session))

    prune_candidates.sort(key=lambda item: item[0])
    considered_candidates = prune_candidates[:limit]
    deleted_assistant_ids: list[str] = []
    delete_errors: dict[str, str] = {}

    for _, assistant_id, session in considered_candidates:
        session = await asyncio.to_thread(
            get_assistant_session,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
        )
        recheck_skip_reason = _terminal_session_prune_recheck_skip_reason(session)
        if recheck_skip_reason is not None:
            skip_reasons[recheck_skip_reason] += 1
            continue

        runtime_state = await _runtime_resource_state(
            assistant_id,
            batch_api=batch_api,
            assistant_session=session,
        )
        runtime_state_cache[assistant_id] = runtime_state
        if not _terminal_session_prune_ready(session, runtime_state):
            skip_reasons["runtime_resources_present"] += 1
            continue

        try:
            deleted = await asyncio.to_thread(
                delete_assistant_session,
                custom_api,
                SETTINGS.default_namespace,
                assistant_id,
            )
        except Exception as exc:
            logger.exception(
                "terminal session prune failed for assistant %s",
                assistant_id,
            )
            delete_errors[assistant_id] = str(exc)
            skip_reasons["delete_failed"] += 1
            continue

        if deleted:
            deleted_assistant_ids.append(assistant_id)
        else:
            skip_reasons["already_deleted"] += 1

    remaining_candidates = max(len(prune_candidates) - len(considered_candidates), 0)
    emit_observability_event(
        "infra.session.prune_terminal.completed",
        retention_hours=effective_retention_hours,
        limit=limit,
        terminal_sessions_found=terminal_sessions_found,
        prune_candidates=len(prune_candidates),
        considered_candidates=len(considered_candidates),
        remaining_candidates=remaining_candidates,
        ghost_healed_count=len(ghost_healed_assistant_ids),
        deleted_count=len(deleted_assistant_ids),
        delete_error_count=len(delete_errors),
        skip_reasons=dict(skip_reasons),
    )
    return {
        "retention_hours": effective_retention_hours,
        "limit": limit,
        "terminal_sessions_found": terminal_sessions_found,
        "prune_candidates": len(prune_candidates),
        "considered_candidates": len(considered_candidates),
        "remaining_candidates": remaining_candidates,
        "ghost_healed_count": len(ghost_healed_assistant_ids),
        "ghost_healed_assistant_ids": ghost_healed_assistant_ids,
        "deleted_count": len(deleted_assistant_ids),
        "deleted_assistant_ids": deleted_assistant_ids,
        "skip_reasons": dict(skip_reasons),
        "delete_errors": delete_errors,
    }


@router.post("/runtime/{assistant_id}/request-desktop")
async def request_desktop_binding_endpoint(assistant_id: str):
    """Promote a voice-only activation to require managed desktop binding.

    Used after a voice call reaches ``ready_to_speak`` so VM assignment and
    file sync run off the call-connect critical path.
    """
    custom_api = await asyncio.to_thread(get_custom_objects_api)
    session = await asyncio.to_thread(
        get_assistant_session,
        custom_api,
        SETTINGS.default_namespace,
        assistant_id,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="AssistantSession not found")

    desktop_mode = session_desktop_mode(session)
    if desktop_mode not in ("ubuntu", "windows"):
        return {"accepted": False, "reason": "desktop_not_configured"}

    if session_desktop_required(session):
        return {"accepted": True, "reason": "already_required"}

    if assistant_session_desired_state(session) != DESIRED_STATE_RUNNING:
        raise HTTPException(
            status_code=409,
            detail="AssistantSession is not in Running desired state",
        )

    await asyncio.to_thread(
        patch_assistant_session_spec,
        custom_api,
        SETTINGS.default_namespace,
        assistant_id,
        desktop_required=True,
    )
    emit_observability_event(
        "infra.desktop_binding.requested",
        assistant_id=assistant_id,
        session_name=assistant_session_name(assistant_id),
        desktop_mode=desktop_mode,
    )
    return {"accepted": True, "reason": "promoted"}


@router.get("/runtime/{assistant_id}")
async def runtime_status_endpoint(
    assistant_id: str,
    binding_id: str | None = Query(default=None),
):
    """Report assistant-scoped or binding-scoped runtime cleanup state."""
    batch_api, _, _, _ = await _get_k8s_clients()
    custom_api = await asyncio.to_thread(get_custom_objects_api)
    assistant_session = None
    if custom_api is not None:
        assistant_session = await asyncio.to_thread(
            get_assistant_session,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
        )
    runtime_state = await _runtime_resource_state(
        assistant_id,
        batch_api=batch_api,
        assistant_session=assistant_session,
    )
    session_phase = str(
        ((assistant_session or {}).get("status") or {}).get("phase", "") or "",
    )
    session_desired_state = (
        assistant_session_desired_state(assistant_session)
        if assistant_session is not None
        else ""
    )
    session_cleanup_complete = assistant_session is None or (
        session_desired_state == DESIRED_STATE_STOPPED and session_phase == "Released"
    )
    runtime_cleanup_complete = (
        session_cleanup_complete
        and not runtime_state["active_job_names"]
        and not runtime_state["active_offline_job_names"]
        and not runtime_state["owned_vms"]
        and not runtime_state["other_owned_vms"]
        and runtime_state["disk_vm_name"] is None
    )
    response = {
        "assistant_id": assistant_id,
        "assistant_session_exists": assistant_session is not None,
        "assistant_session_phase": session_phase or None,
        "assistant_session_desired_state": session_desired_state or None,
        "active_job_names": runtime_state["active_job_names"],
        "active_offline_job_names": runtime_state["active_offline_job_names"],
        "owned_vms": runtime_state["owned_vms"],
        "other_owned_vms": runtime_state["other_owned_vms"],
        "disk_vm_name": runtime_state["disk_vm_name"],
        "runtime_cleanup_complete": runtime_cleanup_complete,
    }
    if binding_id:
        binding_runtime_state = await _binding_runtime_resource_state(
            assistant_id,
            binding_id=binding_id,
            batch_api=batch_api,
        )
        binding_status = _binding_release_status(assistant_session, binding_id)
        binding_runtime_cleanup_complete = (
            (assistant_session is None or binding_status["binding_release_recorded"])
            and not binding_runtime_state["active_job_names"]
            and not binding_runtime_state["owned_vms"]
        )
        response.update(
            {
                "binding_id": binding_id,
                "binding_release_recorded": binding_status["binding_release_recorded"],
                "binding_release_requested_at": binding_status[
                    "binding_release_requested_at"
                ],
                "binding_release_completed_at": binding_status[
                    "binding_release_completed_at"
                ],
                "binding_active_job_names": binding_runtime_state["active_job_names"],
                "binding_owned_vms": binding_runtime_state["owned_vms"],
                "binding_runtime_cleanup_complete": binding_runtime_cleanup_complete,
            },
        )
    return response


@router.get("/vm/pool/status", response_model=PoolStatusResponse)
async def pool_status_endpoint(vm_type: str = None):
    """List all pool VMs with their current state."""
    vms = await asyncio.to_thread(list_pool_vms, vm_type)

    pool_vms = [PoolVMStatus(**vm) for vm in vms]
    idle = sum(
        1 for vm in vms if vm["pool_role"] == "idle" and vm["status"] == "RUNNING"
    )
    assigned = sum(1 for vm in vms if vm["pool_role"] == "assigned")
    provisioning = sum(1 for vm in vms if vm["pool_role"] == "provisioning")
    stopped = sum(1 for vm in vms if vm["status"] == "TERMINATED")

    return PoolStatusResponse(
        vms=pool_vms,
        total=len(pool_vms),
        idle=idle,
        assigned=assigned,
        provisioning=provisioning,
        stopped=stopped,
    )


@router.post("/vm/pool/rebalance")
async def rebalance_pool_endpoint(vm_type: str = "ubuntu"):
    """Manually trigger pool rebalance for a VM type."""
    result = await asyncio.to_thread(rebalance_pool, vm_type)
    return result


@router.post("/vm/pool/reconcile-orphans")
async def reconcile_orphaned_vms_endpoint(vm_type: str = "ubuntu"):
    """Release VMs assigned to assistants that no longer have running K8s Jobs.

    Detects orphaned VMs left behind when pods crash without calling
    release_pool_vm. Safe to call on a cron schedule.
    """
    from .vm_helpers import reconcile_orphaned_vms

    batch_api, _, _, _ = await _get_k8s_clients()
    result = await asyncio.to_thread(reconcile_orphaned_vms, batch_api, vm_type)
    return result


@router.post("/vm/pool/reconcile-orphan-disks")
async def reconcile_orphaned_disks_endpoint(
    max_age_hours: int = 72,
    idle_hours: int | None = None,
    hard_cap_hours: int | None = None,
):
    """Garbage-collect unattached ``unity-disk-*`` assistant disks.

    Three deletion branches cover the cost-leak patterns:

    - *max_age_hours* — assistant unhired, detached ≥ this many hours.
    - *idle_hours* — assistant still hired, detached ≥ this many hours,
      and a fresh GCS archive exists (default 30 d).
    - *hard_cap_hours* — escape hatch: delete even without a fresh
      archive once detached ≥ this many hours. ``0`` (default) disables
      this branch and treats missing/stale archives as reasons to keep.

    Safe to call on a cron schedule (e.g. hourly).
    """
    from .vm_config import (
        POOL_ASSISTANT_DISK_IDLE_HOURS,
        POOL_ASSISTANT_DISK_HARD_CAP_HOURS,
    )

    result = await asyncio.to_thread(
        reconcile_orphaned_disks,
        max_age_hours,
        POOL_ASSISTANT_DISK_IDLE_HOURS if idle_hours is None else idle_hours,
        (
            POOL_ASSISTANT_DISK_HARD_CAP_HOURS
            if hard_cap_hours is None
            else hard_cap_hours
        ),
    )
    return result


@router.post("/vm/pool/purge-quarantined")
async def purge_quarantined_vms_endpoint(vm_type: str = "ubuntu"):
    """Delete quarantined VMs that are consuming quota without serving traffic.

    Quarantined VMs are already stopped and excluded from pool operations.
    This endpoint deletes them so replenish_pool can create fresh replacements.
    """
    from .vm_helpers import purge_quarantined_vms

    result = await asyncio.to_thread(purge_quarantined_vms, vm_type)
    return result


@router.post("/cert-renewal")
async def cert_renewal_endpoint():
    """Renew the *.vm.unify.ai wildcard TLS cert if within 30 days of expiry.

    Triggered monthly by Cloud Scheduler. Checks the current cert in
    Secret Manager; if it expires within 30 days (or is missing),
    performs a DNS-01 challenge via Let's Encrypt and updates the secrets.
    """
    from .cert_renewal import renew_if_needed

    result = await asyncio.to_thread(renew_if_needed, days_threshold=30)
    return result


# =============================================================================
# VM Self-Management Endpoints (GCP identity token auth, not admin key)
# =============================================================================

vm_self_router = APIRouter()


@vm_self_router.post("/vm/mark-idle")
async def vm_mark_idle_endpoint(
    claims: dict = Depends(authenticate_vm_identity),
):
    """Mark the calling VM as idle. Authenticated via GCP identity token.

    Only transitions from provisioning or starting — never from assigned
    (which would steal a VM from an active session).  Also rejects the
    request when the VM's GCE status is not RUNNING, which prevents a
    late-arriving mark-idle call from labeling an already-stopped VM as
    idle.
    """
    gce = claims["google"]["compute_engine"]
    vm_name = gce["instance_name"]

    client = compute_v1.InstancesClient()
    vm = await asyncio.to_thread(
        client.get,
        project=SETTINGS.vm_project_id,
        zone=SETTINGS.vm_zone,
        instance=vm_name,
    )

    if vm.status != "RUNNING":
        logger.warning(
            "VM %s tried to mark idle but GCE status=%s (expected RUNNING)",
            vm_name,
            vm.status,
        )
        return {
            "vm_name": vm_name,
            "status": vm.status,
            "skipped": True,
            "reason": "vm_not_running",
        }

    current_role = (vm.labels or {}).get("pool-role", "")
    if current_role not in ("provisioning", "starting"):
        logger.warning(
            "VM %s tried to mark idle but pool-role=%s (expected provisioning/starting)",
            vm_name,
            current_role,
        )
        return {
            "vm_name": vm_name,
            "pool_role": current_role,
            "skipped": True,
        }

    updated = await asyncio.to_thread(
        _set_pool_labels,
        client,
        vm_name,
        {"pool-role": "idle"},
        expected_role=current_role,
    )
    if not updated:
        logger.warning(
            "VM %s skipped mark-idle because pool-role changed from %s",
            vm_name,
            current_role,
        )
        refreshed_vm = await asyncio.to_thread(
            client.get,
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
            instance=vm_name,
        )
        return {
            "vm_name": vm_name,
            "pool_role": (refreshed_vm.labels or {}).get("pool-role", ""),
            "skipped": True,
            "reason": "role_changed",
        }
    logger.info(f"VM {vm_name} marked itself as idle via identity token")
    return {"vm_name": vm_name, "pool_role": "idle"}


@vm_self_router.post("/vm/release-complete")
async def vm_release_complete_endpoint(
    body: VMReleaseCompleteRequest,
    claims: dict = Depends(authenticate_vm_identity),
):
    """Finalize pool release and record session state when available."""
    causal_token = push_causal_context(
        build_causal_context(
            caller="views.release_complete",
            reason="http_request",
        ),
    )
    try:
        gce = claims["google"]["compute_engine"]
        vm_name = gce["instance_name"]
        client = compute_v1.InstancesClient()
        vm = await asyncio.to_thread(
            client.get,
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
            instance=vm_name,
        )
        labels = dict(vm.labels) if vm.labels else {}
        assistant_id = str(labels.get("assistant-id", "") or "")
        current_binding_id = str(labels.get("binding-id", "") or "")
        pool_role = str(labels.get("pool-role", "") or "")
        emit_observability_event(
            "infra.vm_release_complete.received",
            vm_name=vm_name,
            assistant_id=assistant_id or None,
            binding_id=body.binding_id,
            release_generation=body.release_generation,
            current_binding_id=current_binding_id or None,
            pool_role=pool_role or None,
        )
        if not assistant_id or current_binding_id != body.binding_id:
            emit_observability_event(
                "infra.vm_release_complete.skipped",
                vm_name=vm_name,
                assistant_id=assistant_id or None,
                binding_id=body.binding_id,
                release_generation=body.release_generation,
                current_binding_id=current_binding_id or None,
                pool_role=pool_role or None,
                reason="binding_changed",
                skip_stage="vm_labels",
            )
            return {
                "vm_name": vm_name,
                "assistant_id": assistant_id or None,
                "binding_id": body.binding_id,
                "release_generation": body.release_generation,
                "skipped": True,
                "reason": "binding_changed",
            }

        release_result = await asyncio.to_thread(
            complete_pool_vm_release,
            vm_name,
            body.binding_id,
        )
        release_pool_role = str(release_result.get("pool_role", "") or pool_role or "")
        emit_observability_event(
            "infra.vm_release_complete.pool_result",
            vm_name=vm_name,
            assistant_id=assistant_id or None,
            binding_id=body.binding_id,
            release_generation=body.release_generation,
            current_binding_id=current_binding_id or None,
            pool_role=release_pool_role or None,
            release_result=release_result,
        )
        if release_result.get("skipped"):
            emit_observability_event(
                "infra.vm_release_complete.skipped",
                vm_name=vm_name,
                assistant_id=assistant_id or None,
                binding_id=body.binding_id,
                release_generation=body.release_generation,
                current_binding_id=current_binding_id or None,
                pool_role=release_pool_role or None,
                reason=release_result.get("reason") or "pool_finalize_failed",
                skip_stage="pool_finalize",
            )
            return {
                "vm_name": vm_name,
                "assistant_id": assistant_id or None,
                "binding_id": body.binding_id,
                "release_generation": body.release_generation,
                "skipped": True,
                "reason": release_result.get("reason") or "pool_finalize_failed",
            }

        session_name = assistant_session_name(assistant_id)
        custom_api = await asyncio.to_thread(get_custom_objects_api)
        if custom_api is None:
            emit_observability_event(
                "infra.vm_release_complete.signal_skipped",
                vm_name=vm_name,
                assistant_id=assistant_id,
                binding_id=body.binding_id,
                release_generation=body.release_generation,
                current_binding_id=current_binding_id or None,
                session_name=session_name,
                pool_role=release_pool_role or None,
                reason="session_api_unavailable",
                skip_stage="session_client",
            )
            return {
                "vm_name": vm_name,
                "assistant_id": assistant_id,
                "binding_id": body.binding_id,
                "release_generation": body.release_generation,
                "accepted": True,
                "reason": "session_api_unavailable",
            }

        session = await asyncio.to_thread(
            get_assistant_session,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
        )
        if session is None:
            emit_observability_event(
                "infra.vm_release_complete.signal_skipped",
                vm_name=vm_name,
                assistant_id=assistant_id,
                binding_id=body.binding_id,
                release_generation=body.release_generation,
                current_binding_id=current_binding_id or None,
                session_name=session_name,
                pool_role=release_pool_role or None,
                reason="session_missing",
                skip_stage="session_lookup",
            )
            return {
                "vm_name": vm_name,
                "assistant_id": assistant_id,
                "binding_id": body.binding_id,
                "release_generation": body.release_generation,
                "accepted": True,
                "reason": "session_missing",
            }

        binding = session_binding(session)
        current_release_generation = binding_release_generation(binding)
        session_fields = assistant_session_observability_fields(
            session,
            source="views.release_complete",
            assistant_id=assistant_id,
            session_name=session_name,
            vm_name=vm_name,
        )
        if binding_id_from_status(binding) != body.binding_id:
            skipped_fields = {
                **session_fields,
                "binding_id": body.binding_id,
                "release_generation": body.release_generation,
                "current_release_generation": current_release_generation or None,
                "current_binding_id": binding_id_from_status(binding) or None,
                "current_release_requested_at": binding.get("releaseRequestedAt"),
                "current_release_completed_at": binding.get("releaseCompletedAt"),
                "pool_role": release_pool_role or None,
                "reason": "binding_changed",
                "skip_stage": "session_status",
            }
            emit_observability_event(
                "infra.vm_release_complete.signal_skipped",
                **skipped_fields,
            )
            return {
                "vm_name": vm_name,
                "assistant_id": assistant_id,
                "binding_id": body.binding_id,
                "release_generation": body.release_generation,
                "current_binding_id": binding_id_from_status(binding) or None,
                "accepted": True,
                "reason": "binding_changed",
            }

        if (
            body.release_generation is not None
            and current_release_generation > 0
            and body.release_generation != current_release_generation
        ):
            skipped_fields = {
                **session_fields,
                "binding_id": body.binding_id,
                "release_generation": body.release_generation,
                "current_release_generation": current_release_generation,
                "current_binding_id": binding_id_from_status(binding) or None,
                "current_release_requested_at": binding.get("releaseRequestedAt"),
                "current_release_completed_at": binding.get("releaseCompletedAt"),
                "pool_role": release_pool_role or None,
                "reason": "release_generation_changed",
                "skip_stage": "session_status",
            }
            emit_observability_event(
                "infra.vm_release_complete.signal_skipped",
                **skipped_fields,
            )
            return {
                "vm_name": vm_name,
                "assistant_id": assistant_id,
                "binding_id": body.binding_id,
                "release_generation": body.release_generation,
                "current_release_generation": current_release_generation,
                "accepted": True,
                "reason": "release_generation_changed",
            }

        next_release_completed_at = datetime.now(timezone.utc).isoformat()
        signal_release_generation = (
            body.release_generation or current_release_generation or None
        )
        accepted_fields = {
            **session_fields,
            "binding_id": body.binding_id,
            "release_generation": body.release_generation,
            "current_release_generation": current_release_generation or None,
            "signal_release_generation": signal_release_generation,
            "current_binding_id": binding_id_from_status(binding) or None,
            "current_release_requested_at": binding.get("releaseRequestedAt"),
            "current_release_completed_at": binding.get("releaseCompletedAt"),
            "next_release_completed_at": next_release_completed_at,
            "pool_role": release_pool_role or None,
        }
        emit_observability_event(
            "infra.vm_release_complete.accepted",
            **accepted_fields,
        )
        updated_session = await asyncio.to_thread(
            record_assistant_session_signal,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
            signal_name=SIGNAL_VM_RELEASE_COMPLETE,
            payload=build_binding_signal(
                binding_id=body.binding_id,
                state="completed",
                observed_at=next_release_completed_at,
                vmName=vm_name,
                releaseGeneration=signal_release_generation,
            ),
            source="views.release_complete",
        )
        persisted_fields = assistant_session_observability_fields(
            updated_session,
            source="views.release_complete",
            assistant_id=assistant_id,
            session_name=session_name,
            vm_name=vm_name,
        )
        emit_observability_event(
            "infra.vm_release_complete.persisted",
            **persisted_fields,
            release_requested_at=binding.get("releaseRequestedAt"),
            release_completed_at=next_release_completed_at,
            signal_release_generation=signal_release_generation,
            pool_role=release_pool_role or None,
        )
        return {
            "vm_name": vm_name,
            "assistant_id": assistant_id,
            "binding_id": body.binding_id,
            "release_generation": signal_release_generation,
            "accepted": True,
        }
    finally:
        pop_causal_context(causal_token)


@vm_self_router.post("/vm/wipe-metadata-key")
async def vm_wipe_metadata_key_endpoint(
    body: VMWipeMetadataKeyRequest,
    claims: dict = Depends(authenticate_vm_identity),
):
    """Wipe a metadata key on the calling VM. Authenticated via GCP identity token."""
    gce = claims["google"]["compute_engine"]
    vm_name = gce["instance_name"]

    await asyncio.to_thread(_update_instance_metadata, vm_name, {body.key: ""})
    logger.info(f"VM {vm_name} wiped metadata key '{body.key}' via identity token")
    return {"vm_name": vm_name, "key": body.key, "wiped": True}
