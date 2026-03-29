import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import JSONResponse
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
    acquire_assignment_lease,
    release_assignment_lease,
    claim_idle_container,
    publish_pending_startup,
    process_pending_startups,
)
from .vm_helpers import (
    get_dns_hostname,
    _probe_vm_https,
    _set_pool_labels,
    _update_instance_metadata,
    provision_pool_vm,
    start_pool_vm,
    assign_pool_vm,
    release_pool_vm,
    replenish_pool,
    trim_pool,
    rebalance_pool,
    list_pool_vms,
    find_vm_with_disk,
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

ASSIGN_EXECUTOR = ThreadPoolExecutor(max_workers=15, thread_name_prefix="vm-assign")
POOL_MAINTENANCE_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="pool-maint",
)


async def _publish_desktop_ready(assistant_id: str, hostname: str, vm_type: str) -> str:
    """Publish an ``assistant_desktop_ready`` system event via Pub/Sub.

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
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)
        _pubsub_publisher = pubsub_v1.PublisherClient(credentials=creds)
        _pubsub_subscriber = pubsub_v1.SubscriberClient(credentials=creds)
    return _pubsub_publisher, _pubsub_subscriber


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
    Assign an idle container to serve a Unity assistant.

    Uses a K8s Lease for atomic distributed locking: only one concurrent
    caller can assign a container for a given assistant. The startup
    configuration is written to the claimed Job's annotations; the
    container detects the assignment by polling its own Job state.

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
    try:
        batch_api, _, _, coord_api = await _get_k8s_clients()
        sanitized_aid = str(assistant_id).lower().replace("_", "-")

        # ── Check if a container already serves this assistant ────────
        existing = await asyncio.to_thread(
            batch_api.list_namespaced_job,
            namespace=SETTINGS.default_namespace,
            label_selector=f"app=unity,assistant-id={sanitized_aid}",
        )
        already_running = [
            j
            for j in existing.items
            if j.status.active
            and j.status.active > 0
            and not j.metadata.deletion_timestamp
        ]
        if already_running:
            return {
                "success": True,
                "message": "Assistant already has a running container",
                "job_name": already_running[0].metadata.name,
            }

        # ── Acquire assignment Lease (atomic distributed lock) ────────
        holder_id = f"start-job-{uuid.uuid4().hex[:8]}"
        acquired = await asyncio.to_thread(
            acquire_assignment_lease,
            coord_api,
            assistant_id,
            SETTINGS.default_namespace,
            holder_id,
        )
        if not acquired:
            return {
                "success": True,
                "message": "Assistant is already being assigned by another request",
                "assistant_id": assistant_id,
            }

        # ── Build startup config (before claim so it's available for
        #    both the immediate and overflow paths) ─────────────────
        startup_config = json.dumps(
            {
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
                "voice_provider": voice_provider,
                "voice_id": voice_id,
                "desktop_mode": desktop_mode,
                "desktop_url": desktop_url if desktop_url else None,
                "user_desktop_mode": (user_desktop_mode if user_desktop_mode else None),
                "user_desktop_filesys_sync": user_desktop_filesys_sync.lower()
                == "true",
                "user_desktop_url": user_desktop_url if user_desktop_url else None,
                "demo_id": int(demo_id) if demo_id else None,
                "team_ids": json.loads(team_ids) if team_ids else [],
                "org_id": int(org_id) if org_id else None,
            },
        )

        # ── Claim an idle container (labels + startup config) ─────────
        try:
            job_name = await asyncio.to_thread(
                claim_idle_container,
                batch_api,
                assistant_id,
                SETTINGS.default_namespace,
                startup_config,
            )

            # Assign pool VM after container claim, then replenish.
            # If the VM pool is exhausted, queue for deferred retry.
            if desktop_mode in ("windows", "ubuntu"):

                async def _assign_then_replenish(
                    _aid=assistant_id,
                    _key=api_key,
                    _vt=desktop_mode,
                ):
                    from .vm_helpers import publish_pending_vm_assignment

                    loop = asyncio.get_running_loop()
                    try:
                        await loop.run_in_executor(
                            ASSIGN_EXECUTOR,
                            partial(
                                assign_pool_vm,
                                assistant_id=_aid,
                                unify_apikey=_key,
                                vm_type=_vt,
                            ),
                        )
                    except Exception as exc:
                        logger.warning(
                            "VM assignment failed for %s (%s), queuing for retry",
                            _aid,
                            exc,
                        )
                        await loop.run_in_executor(
                            None,
                            partial(
                                publish_pending_vm_assignment,
                                _aid,
                                _key,
                                _vt,
                            ),
                        )
                    loop.run_in_executor(
                        POOL_MAINTENANCE_EXECUTOR,
                        partial(replenish_pool, _vt, extra_demand=1),
                    )

                asyncio.create_task(_assign_then_replenish())

            return {
                "success": True,
                "message": "Container assigned to assistant",
                "job_name": job_name,
                "assistant_id": assistant_id,
            }
        finally:
            await asyncio.to_thread(
                release_assignment_lease,
                coord_api,
                assistant_id,
                SETTINGS.default_namespace,
            )

    except RuntimeError:
        # Pool exhausted — publish to the durable pending queue so the
        # reconciler can assign a container when capacity is available.
        await asyncio.to_thread(publish_pending_startup, startup_config)
        return JSONResponse(
            status_code=202,
            content={
                "success": True,
                "status": "queued",
                "message": "Startup request queued — pool temporarily exhausted",
                "assistant_id": assistant_id,
            },
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to assign container: {str(e)}",
        )


@router.post("/pending/process")
async def process_pending():
    """Process pending startup requests from the durable Pub/Sub queue.

    Pulls messages published by /infra/job/start when the pool was
    exhausted, and assigns them to idle containers using the same
    Lease + CAS mechanism.  Stateless and idempotent — triggered by
    the 1-minute Cloud Scheduler cron and reactively after pool
    replenishment.
    """
    try:
        batch_api, _, _, coord_api = await _get_k8s_clients()
        result = await asyncio.to_thread(
            process_pending_startups,
            batch_api,
            coord_api,
            SETTINGS.default_namespace,
        )
        return {"success": True, **result}
    except Exception as e:
        logger.exception("Error processing pending startups")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process pending startups: {str(e)}",
        )


# stop kubernetes job
@router.post("/job/stop")
async def stop_job(
    job_name: str = Form(...),
    namespace: str = Form(SETTINGS.default_namespace),
):
    """
    Stop a Kubernetes Job for a Unity assistant.
    """
    try:
        batch_api, core_api, networking_api, _coord = await _get_k8s_clients()

        success = await asyncio.to_thread(suspend_job, batch_api, job_name, namespace)
        if success:
            return {
                "success": True,
                "message": f"Job suspended successfully: {job_name}",
            }
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to suspend job: {job_name}",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to suspend job: {str(e)}")


# list kubernetes jobs
@router.get("/jobs")
async def list_kubernetes_jobs(
    namespace: str = SETTINGS.default_namespace,
    hours: int = 8,
    label_selector: str = "app=unity",
):
    """
    List all Unity Kubernetes jobs in the namespace.

    Args:
        namespace: Kubernetes namespace (optional, defaults to "default")
        hours: Number of hours to filter jobs (optional, defaults to 3)
        label_selector: K8s label selector (optional, defaults to "app=unity")
    """
    try:
        batch_api, core_api, networking_api, _coord = await _get_k8s_clients()

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)
        relevant_dates = sorted(
            {
                cutoff.strftime("%Y-%m-%d"),
                now.strftime("%Y-%m-%d"),
            },
        )
        date_filter = f"unity-date in ({','.join(relevant_dates)})"
        full_selector = (
            f"{label_selector},{date_filter}" if label_selector else date_filter
        )

        jobs = await asyncio.to_thread(
            batch_api.list_namespaced_job,
            namespace=namespace,
            label_selector=full_selector,
        )
        job_items = list(
            filter(
                lambda job: (
                    now
                    - datetime.strptime(
                        "-".join(
                            filter(
                                lambda part: part.isdigit() and len(part) in [2, 4],
                                job.metadata.name.split("-"),
                            ),
                        ),
                        "%Y-%m-%d-%H-%M-%S",
                    ).replace(tzinfo=timezone.utc)
                )
                < timedelta(hours=hours),
                jobs.items,
            ),
        )
        print(f"Job items: {list(map(lambda job: job.metadata.name, job_items))}")

        job_list = []
        for job in job_items:
            assistant_id = job.metadata.labels.get("assistant-id", "unknown")
            status = "Unknown"

            if job.status.active:
                status = "Running"
            elif job.status.succeeded:
                status = "Completed"
            elif job.status.failed:
                status = "Failed"

            job_info = {
                "job_name": job.metadata.name,
                "assistant_id": assistant_id,
                "labels": job.metadata.labels,
                "status": status,
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
            job_list.append(job_info)

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
        # Get credentials from environment variable
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        creds = Credentials.from_service_account_info(creds_json)

        # Initialize the Cloud Storage client
        storage_client = storage.Client(credentials=creds)

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
    vm_type = request_body.vm_type

    # Pool VMs pass their own hostname; legacy VMs derive it from assistant_id
    hostname = request_body.hostname or get_dns_hostname(assistant_id)
    reachable = await asyncio.to_thread(_probe_vm_https, hostname)
    if not reachable:
        logger.warning(
            f"VM HTTPS probe failed for {hostname} (assistant {assistant_id}), "
            "not publishing assistant_desktop_ready",
        )
        raise HTTPException(
            status_code=503,
            detail=f"VM HTTPS not reachable at {hostname}",
        )

    message_id = await _publish_desktop_ready(assistant_id, hostname, vm_type)

    return {
        "success": True,
        "message_id": message_id,
        "assistant_id": assistant_id,
    }


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
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to assign pool VM: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/vm/pool/release")
async def release_pool_endpoint(request: PoolReleaseRequest):
    """Release a pool VM back to idle.

    Clears assignment metadata (triggering watcher cleanup), detaches
    the persistent disk, and resets labels. Idempotent.
    """
    try:
        result = await asyncio.to_thread(release_pool_vm, request.assistant_id)

        # Trim: stop excess idle VMs now that one was returned (fire-and-forget)
        vm_type = result.get("vm_type", "ubuntu")
        asyncio.get_running_loop().run_in_executor(
            POOL_MAINTENANCE_EXECUTOR,
            partial(trim_pool, vm_type),
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
        return {"assistant_id": assistant_id, "deleted": deleted}
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


@router.post("/vm/pending/process")
async def process_pending_vm_assignments_endpoint():
    """Process pending VM assignment requests from the durable Pub/Sub queue.

    Pulls messages published when assign_pool_vm fails due to pool
    exhaustion, and retries assignment.  Stateless and idempotent —
    triggered every minute by Cloud Scheduler and reactively after
    VM pool replenishment.
    """
    from .vm_helpers import process_pending_vm_assignments

    try:
        result = await asyncio.to_thread(process_pending_vm_assignments)
        return {"success": True, **result}
    except Exception as e:
        logger.exception("Error processing pending VM assignments")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process pending VM assignments: {str(e)}",
        )


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
    (which would steal a VM from an active session).
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

    await asyncio.to_thread(
        _set_pool_labels,
        client,
        vm_name,
        {"pool-role": "idle"},
    )
    logger.info(f"VM {vm_name} marked itself as idle via identity token")
    return {"vm_name": vm_name, "pool_role": "idle"}


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
