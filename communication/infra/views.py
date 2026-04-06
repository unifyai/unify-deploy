import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from functools import partial
from google.api_core.exceptions import Conflict, NotFound as GcpNotFound
from google.cloud import compute_v1, pubsub_v1, storage
from google.oauth2.service_account import Credentials
from google.protobuf import duration_pb2
import json
import logging
import os
import time
import uuid
from kubernetes.client.rest import ApiException
from .helpers import (
    setup_kubernetes_client,
    create_unity_job,
    delete_job,
    get_job_logs,
    patch_job_labels,
    suspend_job,
)
from .assistant_sessions import (
    ACTIVE_PHASES,
    DESIRED_STATE_STOPPED,
    SIGNAL_DESKTOP_READY,
    SIGNAL_VM_RELEASE_COMPLETE,
    TERMINAL_PHASES,
    assistant_session_desired_state,
    assistant_session_observability_fields,
    assistant_session_name,
    binding_desktop_url,
    binding_id as binding_id_from_status,
    binding_job_ref,
    binding_pod_ref,
    binding_vm_ref,
    build_assistant_session_spec,
    build_binding_signal,
    delete_assistant_session,
    create_or_update_assistant_session,
    create_or_update_bootstrap_secret,
    emit_observability_event,
    get_assistant_session,
    get_custom_objects_api,
    patch_assistant_session_spec,
    read_bootstrap_secret,
    record_assistant_session_signal,
    session_binding,
    vm_refs_match,
)
from .observability import (
    build_causal_context,
    pop_causal_context,
    push_causal_context,
)
from .vm_helpers import (
    AssistantDiskInUseError,
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
TERMINAL_SESSION_PRUNE_PREVIEW_RETENTION_HOURS = 6.0
TERMINAL_SESSION_PRUNE_DEFAULT_RETENTION_HOURS = 24.0

ASSIGN_EXECUTOR = ThreadPoolExecutor(max_workers=15, thread_name_prefix="vm-assign")
POOL_MAINTENANCE_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="pool-maint",
)


def _service_account_credentials() -> Credentials:
    """Build GCP service-account credentials from the configured env payload."""
    creds_json = os.getenv("GCP_SA_KEY")
    if not creds_json:
        raise RuntimeError("GCP_SA_KEY must be set for GCP-backed infra endpoints")
    return Credentials.from_service_account_info(json.loads(creds_json))


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


async def _get_k8s_clients():
    """Return cached K8s API clients, running the (potentially blocking)
    setup in a thread so the event loop is never stalled."""
    result = await asyncio.to_thread(setup_kubernetes_client)
    if not result[0]:
        raise HTTPException(
            status_code=500,
            detail="Failed to connect to Kubernetes cluster",
        )
    return result


router = APIRouter()


_pubsub_publisher: pubsub_v1.PublisherClient | None = None
_pubsub_subscriber: pubsub_v1.SubscriberClient | None = None


def _get_pubsub_clients() -> (
    tuple[pubsub_v1.PublisherClient, pubsub_v1.SubscriberClient]
):
    """Return cached PubSub publisher and subscriber clients."""
    global _pubsub_publisher, _pubsub_subscriber
    if _pubsub_publisher is None or _pubsub_subscriber is None:
        creds = _service_account_credentials()
        _pubsub_publisher = pubsub_v1.PublisherClient(credentials=creds)
        _pubsub_subscriber = pubsub_v1.SubscriberClient(credentials=creds)
    return _pubsub_publisher, _pubsub_subscriber


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
    user_whatsapp_number: str,
    assistant_whatsapp_number: str,
    voice_provider: str,
    voice_id: str,
    desktop_mode: str,
    desktop_url: str,
    user_desktop_mode: str,
    user_desktop_filesys_sync: str,
    user_desktop_url: str,
    demo_id: str,
    team_ids: str,
    org_id: str,
) -> dict:
    """Build the bootstrap Secret payload for a session activation request.

    `/infra/job/start` may reuse a non-terminal AssistantSession, so callers
    must always pass the latest assistant config. The bootstrap Secret becomes
    the controller's source of truth for both fresh and reused activations.
    """
    return {
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
        "assistant_timezone": assistant_timezone,
        "user_number": user_number,
        "assistant_number": assistant_number,
        "assistant_email": assistant_email,
        "user_whatsapp_number": user_whatsapp_number,
        "assistant_whatsapp_number": assistant_whatsapp_number,
        "voice_provider": voice_provider,
        "voice_id": voice_id,
        "desktop_mode": desktop_mode,
        "desktop_url": desktop_url if desktop_url else None,
        "user_desktop_mode": user_desktop_mode if user_desktop_mode else None,
        "user_desktop_filesys_sync": user_desktop_filesys_sync.lower() == "true",
        "user_desktop_url": user_desktop_url if user_desktop_url else None,
        "demo_id": int(demo_id) if demo_id else None,
        "team_ids": json.loads(team_ids) if team_ids else [],
        "org_id": int(org_id) if org_id else None,
    }


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


# create pubsub topic
@router.post("/pubsub/topic")
async def create_pubsub_topic(topic_name: str = Form(...)):
    """
    Create a Google Cloud Pub/Sub topic and subscription with the assistant_id as
    the name.
    """
    try:
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
            "success": True,
            "message": "Topic and subscriptions ensured with no expiration",
            "topic_name": topic_path,
            "subscription_name": subscription_path,
            "actions_subscription_name": actions_subscription_path,
            "system_error_subscription_name": system_error_subscription_path,
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
    assistant_age: str = Form(...),
    assistant_nationality: str = Form(...),
    assistant_about: str = Form(...),
    assistant_timezone: str = Form("UTC"),
    user_number: str = Form(""),
    assistant_number: str = Form(""),
    assistant_email: str = Form(""),
    user_whatsapp_number: str = Form(""),
    assistant_whatsapp_number: str = Form(""),
    voice_provider: str = Form(""),
    voice_id: str = Form(""),
    desktop_mode: str = Form("ubuntu"),
    desktop_url: str = Form(""),
    user_desktop_mode: str = Form(""),
    user_desktop_filesys_sync: str = Form("false"),
    user_desktop_url: str = Form(""),
    demo_id: str = Form(""),
    team_ids: str = Form(""),
    org_id: str = Form(""),
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
        assistant_age: Assistant's age (required)
        assistant_nationality: Assistant's nationality (required)
        assistant_about: Assistant's about (required)
        assistant_timezone: Assistant's timezone (required)
        user_number: User's phone number (optional, defaults to empty string)
        assistant_number: Assistant's phone number (optional, defaults to empty string)
        assistant_email: Assistant's email (optional, defaults to empty string)
        user_whatsapp_number: User's whatsapp number (optional, defaults to empty string)
        assistant_whatsapp_number: Assistant's WhatsApp pool number (optional, defaults to empty string)
        voice_provider: TTS provider (optional, defaults to empty string)
        voice_id: Voice ID (optional, defaults to empty string)
        desktop_mode: Desktop mode - ubuntu/windows/macos (optional, defaults to "ubuntu")
        desktop_url: URL to access the VM desktop (optional, defaults to empty string)
        user_desktop_mode: User's own desktop mode - ubuntu/windows/macos (optional)
        user_desktop_filesys_sync: Whether to sync user desktop filesystem (optional, defaults to "false")
        user_desktop_url: URL to user's own desktop (optional)
        demo_id: Demo assistant metadata ID (optional, empty string if not a demo)
        team_ids: JSON-encoded list of team IDs the user belongs to (optional, defaults to empty)
        org_id: Organization ID if this is an organizational assistant (optional, defaults to empty)
    """
    session_name = assistant_session_name(assistant_id)
    activation_id = None
    existing_phase = None
    reused_active_session = False
    restart_in_progress = False
    causal_token = push_causal_context(
        build_causal_context(
            caller="views.job_start",
            reason="http_request",
        ),
    )

    try:
        _, core_api, _, _ = await _get_k8s_clients()
        custom_api = await asyncio.to_thread(get_custom_objects_api)
        if custom_api is None:
            raise HTTPException(
                status_code=500,
                detail="Failed to initialize AssistantSession API client",
            )

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
            assistant_timezone=assistant_timezone,
            user_number=user_number,
            assistant_number=assistant_number,
            assistant_email=assistant_email,
            user_whatsapp_number=user_whatsapp_number,
            assistant_whatsapp_number=assistant_whatsapp_number,
            voice_provider=voice_provider,
            voice_id=voice_id,
            desktop_mode=desktop_mode,
            desktop_url=desktop_url,
            user_desktop_mode=user_desktop_mode,
            user_desktop_filesys_sync=user_desktop_filesys_sync,
            user_desktop_url=user_desktop_url,
            demo_id=demo_id,
            team_ids=team_ids,
            org_id=org_id,
        )
        existing_session = await asyncio.to_thread(
            get_assistant_session,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
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
            startup_payload,
        )

        spec = build_assistant_session_spec(
            assistant_id=assistant_id,
            user_id=user_id,
            medium=medium,
            desktop_mode=desktop_mode,
            startup_secret_ref=secret_name,
            activation_id=activation_id,
        )
        try:
            session = await asyncio.to_thread(
                create_or_update_assistant_session,
                custom_api,
                SETTINGS.default_namespace,
                assistant_id,
                spec,
            )
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
            spec = build_assistant_session_spec(
                assistant_id=assistant_id,
                user_id=user_id,
                medium=medium,
                desktop_mode=desktop_mode,
                startup_secret_ref=secret_name,
                activation_id=activation_id,
            )
            session = await asyncio.to_thread(
                create_or_update_assistant_session,
                custom_api,
                SETTINGS.default_namespace,
                assistant_id,
                spec,
            )
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
        status = session.get("status", {})
        binding = session_binding(session)
        emit_observability_event(
            "infra.job_start.ensured",
            **assistant_session_observability_fields(
                session,
                reused_active_session=reused_active_session,
                startup_secret_ref=secret_name,
            ),
        )
        return {
            "success": True,
            "message": "AssistantSession ensured",
            "assistant_id": assistant_id,
            "session_name": session_name,
            "activation_id": activation_id,
            "phase": status.get("phase", "PendingJob"),
            "job_name": binding_job_ref(binding).get("name"),
        }
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
            }

        binding = session_binding(session)
        emit_observability_event(
            "infra.session.stop.requested",
            **assistant_session_observability_fields(
                session,
                assistant_id=assistant_id,
            ),
            release_requested_at=binding.get("releaseRequestedAt"),
            release_completed_at=binding.get("releaseCompletedAt"),
        )
        updated = await asyncio.to_thread(
            patch_assistant_session_spec,
            custom_api,
            SETTINGS.default_namespace,
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
            previous_binding_id=binding_id_from_status(binding) or None,
        )
        return {
            "success": True,
            "assistant_id": assistant_id,
            "stopped": True,
            "desired_state": ((updated.get("spec") or {}).get("desiredState") or ""),
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

        success = await asyncio.to_thread(suspend_job, batch_api, job_name, namespace)
        if success:
            emit_observability_event(
                "infra.job_stop.accepted",
                job_name=job_name,
                namespace=namespace,
            )
            return {
                "success": True,
                "message": f"Job suspended successfully: {job_name}",
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
        from .vm_config import POOL_VM_NAME_PREFIX

        candidate = f"{POOL_VM_NAME_PREFIX}-{request.vm_type}-{n}{SETTINGS.env_suffix}"
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

    vm_name = str(binding_vm_ref(binding).get("name", "") or "")
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
        )
        if result.get("retired"):
            asyncio.get_running_loop().run_in_executor(
                POOL_MAINTENANCE_EXECUTOR,
                partial(replenish_pool, result.get("vm_type", "ubuntu")),
            )
        return result
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


def _default_terminal_session_prune_retention_hours() -> float:
    """Return the environment-specific retention window for terminal sessions."""

    if SETTINGS.deploy_env == "preview":
        return TERMINAL_SESSION_PRUNE_PREVIEW_RETENTION_HOURS
    return TERMINAL_SESSION_PRUNE_DEFAULT_RETENTION_HOURS


async def _runtime_resource_state(
    assistant_id: str,
    *,
    batch_api,
    assistant_session: dict | None,
) -> dict[str, object]:
    """Return the live runtime resources currently owned by an assistant."""

    sanitized = assistant_id.lower().replace("_", "-")
    jobs = await asyncio.to_thread(
        batch_api.list_namespaced_job,
        namespace=SETTINGS.default_namespace,
        label_selector=f"app=unity,assistant-id={sanitized}",
    )
    active_job_names = [
        job.metadata.name
        for job in jobs.items
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
        "owned_vms": owned_vms,
        "other_owned_vms": other_owned_vms,
        "disk_vm_name": disk_vm_name,
        "current_binding_id": current_binding_id,
    }


def _terminal_session_prune_ready(
    assistant_session: dict | None,
    runtime_state: dict[str, object],
) -> bool:
    """Return whether a terminal session can be safely deleted."""

    phase = str(((assistant_session or {}).get("status") or {}).get("phase", "") or "")
    return (
        phase in TERMINAL_PHASES
        and not runtime_state["current_binding_id"]
        and not runtime_state["active_job_names"]
        and not runtime_state["owned_vms"]
        and not runtime_state["other_owned_vms"]
        and runtime_state["disk_vm_name"] is None
    )


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
    cutoff = datetime.now(timezone.utc) - timedelta(hours=effective_retention_hours)
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
        runtime_state = await _runtime_resource_state(
            assistant_id,
            batch_api=batch_api,
            assistant_session=session,
        )
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
        "deleted_count": len(deleted_assistant_ids),
        "deleted_assistant_ids": deleted_assistant_ids,
        "skip_reasons": dict(skip_reasons),
        "delete_errors": delete_errors,
    }


@router.get("/runtime/{assistant_id}")
async def runtime_status_endpoint(assistant_id: str):
    """Report whether an assistant still has live runtime resources."""
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
        and not runtime_state["owned_vms"]
        and not runtime_state["other_owned_vms"]
        and runtime_state["disk_vm_name"] is None
    )

    return {
        "assistant_id": assistant_id,
        "assistant_session_exists": assistant_session is not None,
        "assistant_session_phase": session_phase or None,
        "assistant_session_desired_state": session_desired_state or None,
        "active_job_names": runtime_state["active_job_names"],
        "owned_vms": runtime_state["owned_vms"],
        "other_owned_vms": runtime_state["other_owned_vms"],
        "disk_vm_name": runtime_state["disk_vm_name"],
        "runtime_cleanup_complete": runtime_cleanup_complete,
    }


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
    """Record release completion for the current binding."""
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
            current_binding_id=current_binding_id or None,
            pool_role=pool_role or None,
        )
        if not assistant_id or current_binding_id != body.binding_id:
            emit_observability_event(
                "infra.vm_release_complete.skipped",
                vm_name=vm_name,
                assistant_id=assistant_id or None,
                binding_id=body.binding_id,
                current_binding_id=current_binding_id or None,
                pool_role=pool_role or None,
                reason="binding_changed",
                skip_stage="vm_labels",
            )
            return {
                "vm_name": vm_name,
                "assistant_id": assistant_id or None,
                "binding_id": body.binding_id,
                "skipped": True,
                "reason": "binding_changed",
            }

        custom_api = await asyncio.to_thread(get_custom_objects_api)
        if custom_api is None:
            raise HTTPException(
                status_code=500,
                detail="Failed to initialize AssistantSession API client",
            )
        session_name = assistant_session_name(assistant_id)
        session = await asyncio.to_thread(
            get_assistant_session,
            custom_api,
            SETTINGS.default_namespace,
            assistant_id,
        )
        if session is None:
            emit_observability_event(
                "infra.vm_release_complete.skipped",
                vm_name=vm_name,
                assistant_id=assistant_id,
                binding_id=body.binding_id,
                current_binding_id=current_binding_id or None,
                session_name=session_name,
                pool_role=pool_role or None,
                reason="session_missing",
                skip_stage="session_lookup",
            )
            return {
                "vm_name": vm_name,
                "assistant_id": assistant_id,
                "binding_id": body.binding_id,
                "skipped": True,
                "reason": "session_missing",
            }

        binding = session_binding(session)
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
                "current_binding_id": binding_id_from_status(binding) or None,
                "current_release_requested_at": binding.get("releaseRequestedAt"),
                "current_release_completed_at": binding.get("releaseCompletedAt"),
                "pool_role": pool_role or None,
                "reason": "binding_changed",
                "skip_stage": "session_status",
            }
            emit_observability_event(
                "infra.vm_release_complete.skipped",
                **skipped_fields,
            )
            return {
                "vm_name": vm_name,
                "assistant_id": assistant_id,
                "binding_id": body.binding_id,
                "current_binding_id": binding_id_from_status(binding) or None,
                "skipped": True,
                "reason": "binding_changed",
            }

        next_release_completed_at = datetime.now(timezone.utc).isoformat()
        accepted_fields = {
            **session_fields,
            "binding_id": body.binding_id,
            "current_binding_id": binding_id_from_status(binding) or None,
            "current_release_requested_at": binding.get("releaseRequestedAt"),
            "current_release_completed_at": binding.get("releaseCompletedAt"),
            "next_release_completed_at": next_release_completed_at,
            "pool_role": pool_role or None,
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
            pool_role=pool_role or None,
        )
        return {
            "vm_name": vm_name,
            "assistant_id": assistant_id,
            "binding_id": body.binding_id,
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
