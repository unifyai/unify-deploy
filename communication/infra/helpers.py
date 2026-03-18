from datetime import datetime, timezone
import json
import logging
import os
import subprocess
import threading
import uuid
from kubernetes import client as k8s_client, config
from kubernetes.client.rest import ApiException

from communication.helpers import ADAPTERS_URL, COMMS_URL, ORCHESTRA_URL

logger = logging.getLogger(__name__)

_k8s_clients: tuple | None = None
_k8s_lock = threading.Lock()


def setup_kubernetes_client():
    """Initialize Kubernetes client using GKE authentication.

    The clients are cached after the first successful setup so that subsequent
    calls return instantly instead of re-running gcloud auth subprocesses.

    Returns:
        tuple: (BatchV1Api, CoreV1Api, NetworkingV1Api, CoordinationV1Api)
        tuple: (None, None, None, None) - If setup fails
    """
    global _k8s_clients

    if _k8s_clients is not None:
        return _k8s_clients

    with _k8s_lock:
        # Double-check after acquiring lock
        if _k8s_clients is not None:
            return _k8s_clients

        try:
            print("Starting Kubernetes client setup...")

            creds_json = os.getenv("GCP_SA_KEY")
            if not creds_json:
                print("❌ GCP_SA_KEY environment variable not set")
                return None, None, None

            creds_data = json.loads(creds_json)
            project_id = creds_data.get("project_id", "gcp-project-runtime")
            service_account_email = creds_data.get("client_email", "unknown")

            print(f"🔑 Using service account: {service_account_email}")
            print(f"🏗️  Project: {project_id}")

            cluster_name = "unity"
            region = "us-central1"

            print("🔐 Setting up GKE authentication...")
            sa_key_path = "/tmp/gcp-sa-key.json"
            with open(sa_key_path, "w") as f:
                f.write(creds_json)

            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_key_path

            print("🔑 Authenticating with gcloud...")
            subprocess.run(
                [
                    "gcloud",
                    "auth",
                    "activate-service-account",
                    "--key-file",
                    sa_key_path,
                    "--quiet",
                ],
                check=True,
                capture_output=True,
            )

            print("🔗 Getting cluster credentials...")
            subprocess.run(
                [
                    "gcloud",
                    "container",
                    "clusters",
                    "get-credentials",
                    cluster_name,
                    "--region",
                    region,
                    "--project",
                    project_id,
                    "--quiet",
                ],
                check=True,
                capture_output=True,
            )

            print(
                f"🔑 Using service account: {creds_data.get('client_email', 'unknown')}",
            )

            _gke_credentials = google_sa.Credentials.from_service_account_info(
                creds_data,
                scopes=_GKE_SCOPES,
            )
            auth_request = google.auth.transport.requests.Request()
            _gke_credentials.refresh(auth_request)

            gke_svc = _build_gke_svc(
                "container",
                "v1",
                credentials=_gke_credentials,
                cache_discovery=False,
            )
            cluster = (
                gke_svc.projects()
                .locations()
                .clusters()
                .get(
                    name=f"projects/{project_id}/locations/{region}/clusters/{cluster_name}",
                )
                .execute()
            )

            endpoint = cluster["endpoint"]
            ca_cert_b64 = cluster["masterAuth"]["clusterCaCertificate"]

            ca_file = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            ca_file.write(base64.b64decode(ca_cert_b64))
            ca_file.close()
            _ca_cert_path = ca_file.name

            configuration = k8s_client.Configuration()
            configuration.host = f"https://{endpoint}"
            configuration.ssl_ca_cert = _ca_cert_path
            configuration.api_key_prefix["authorization"] = "Bearer"
            configuration.api_key["authorization"] = _gke_credentials.token

            api_client = _GKEApiClient(configuration, _gke_credentials)

            print("🔗 Creating API clients...")
            batch_api = k8s_client.BatchV1Api()
            core_api = k8s_client.CoreV1Api()
            networking_api = k8s_client.NetworkingV1Api()
            coord_api = k8s_client.CoordinationV1Api()

            print("Kubernetes client setup complete!")

            _k8s_clients = (batch_api, core_api, networking_api, coord_api)
            return _k8s_clients

        except Exception as e:
            print(f"Error setting up Kubernetes client: {e}")
            return None, None, None, None


def delete_job(
    batch_api,
    job_name: str,
    namespace: str = "default",
    resource_version: str | None = None,
):
    """Delete a Unity job.

    Args:
        resource_version: If provided, the job's resourceVersion must match this
            exactly or the deletion is skipped (returns False). Guards against
            TOCTOU races where a job's status changes between listing and deletion.
    """
    try:
        # Use preconditions to guard against TOCTOU races. If the job's
        # resourceVersion has changed (e.g. label patched to 'running'),
        # the delete will fail with 409 Conflict.
        delete_options = k8s_client.V1DeleteOptions(
            propagation_policy="Background",
        )
        if resource_version:
            delete_options.preconditions = k8s_client.V1Preconditions(
                resource_version=resource_version,
            )

        batch_api.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            body=delete_options,
        )

        print(f"✅ Job deleted successfully: {job_name}")
        return True

    except ApiException as e:
        if e.status == 404:
            print(f"⚠️  Job not found (already deleted): {job_name}")
            return True
        elif e.status == 409:
            print(
                f"⏭️  Skipping delete for {job_name}: "
                f"Conflict (resource modified since last check)",
            )
            return False
        else:
            print(f"❌ Error deleting job: {e}")
            return False


def read_job(
    batch_api,
    job_name: str,
    namespace: str = "default",
) -> dict | None:
    """Read a single Unity job's metadata. Returns None if not found."""
    try:
        job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
        return {
            "job_name": job.metadata.name,
            "labels": dict(job.metadata.labels or {}),
            "resource_version": job.metadata.resource_version,
            "active": job.status.active or 0,
        }
    except ApiException as e:
        if e.status == 404:
            return None
        print(f"❌ Error reading job {job_name}: {e}")
        return None


def patch_job_labels(
    batch_api,
    job_name: str,
    labels: dict,
    namespace: str = "default",
):
    """Patch labels on an existing Unity job."""
    try:
        body = {"metadata": {"labels": labels}}
        batch_api.patch_namespaced_job(
            name=job_name,
            namespace=namespace,
            body=body,
        )
        print(f"✅ Job labels patched: {job_name} -> {labels}")
        return True
    except ApiException as e:
        if e.status == 404:
            print(f"⚠️  Job not found: {job_name}")
            return False
        else:
            print(f"❌ Error patching job labels: {e}")
            return False


def create_unity_job(
    batch_api,
    job_name: str,
    namespace: str = "default",
    image: str = "us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity:latest",
    is_staging: bool = False,
    ttl_seconds_after_finished: int = None,
):
    """
    Create a Kubernetes Job for a Unity assistant.

    Args:
        batch_api: Kubernetes Batch API client
        job_name: Name of the job
        namespace: Kubernetes namespace
        image: Docker image to use
        is_staging: Whether to use staging image
        ttl_seconds_after_finished: Seconds after job completion before cleanup (None to disable)
    """
    try:
        # Define the assistant-specific environment variables
        env_vars = [
            {"name": "UNITY_CONVERSATION_JOB_NAME", "value": job_name},
            {
                "name": "GOOGLE_APPLICATION_CREDENTIALS",
                "value": "/secrets/key.json",
            },
            # Startup optimizations
            {"name": "PYTHONUNBUFFERED", "value": "1"},
            {
                "name": "TOKENIZERS_PARALLELISM",
                "value": "false",
            },
            {"name": "OMP_NUM_THREADS", "value": "2"},
            {"name": "MKL_NUM_THREADS", "value": "2"},
            {"name": "HF_HOME", "value": "/tmp/huggingface"},
            {"name": "XDG_CACHE_HOME", "value": "/tmp/.cache"},
            {"name": "EVENTBUS_PUBLISHING_ENABLED", "value": "true"},
            {"name": "EVENTBUS_PUBSUB_STREAMING", "value": "true"},
            {"name": "UNITY_COMMS_URL", "value": COMMS_URL},
            {"name": "UNITY_ADAPTERS_URL", "value": ADAPTERS_URL},
            {"name": "ORCHESTRA_URL", "value": ORCHESTRA_URL},
        ]
        if is_staging:
            env_vars += [{"name": "STAGING", "value": "true"}]

        # Define the job manifest
        job_manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
                "labels": {
                    "app": "unity",
                    "created-by": "create_job_script",
                    "unity-status": "idle",
                    "unity-date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "unity-image-hash": (
                        image.rsplit(":", 1)[-1] if ":" in image else "unknown"
                    ),
                },
            },
            "spec": {
                "backoffLimit": 0,
                "template": {
                    "metadata": {
                        "labels": {"app": "unity"},
                        "annotations": {
                            "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
                        },
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": "comm-sa",
                        "terminationGracePeriodSeconds": 30,  # Faster termination
                        "priorityClassName": "unity-idle",
                        "containers": [
                            {
                                "name": "unity-assistant",
                                "image": image,
                                "imagePullPolicy": "IfNotPresent",  # Use cached images for faster startup
                                "ports": [
                                    {"containerPort": 8000},
                                    {"containerPort": 6379},
                                ],
                                "envFrom": [
                                    {"configMapRef": {"name": "unity-config"}},
                                    {"secretRef": {"name": "unity-secrets"}},
                                ],
                                "env": env_vars,
                                "resources": {
                                    "requests": {
                                        "cpu": "2",
                                        "memory": "16Gi",
                                        "ephemeral-storage": "10Gi",
                                    },
                                    "limits": {
                                        "cpu": "2",
                                        "memory": "16Gi",
                                        "ephemeral-storage": "10Gi",
                                    },
                                },
                                "volumeMounts": [
                                    {
                                        "name": "sa-key",
                                        "mountPath": "/secrets",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "tmp-vol",
                                        "mountPath": "/tmp",
                                    },
                                ],
                            },
                        ],
                        "volumes": [
                            {"name": "sa-key", "secret": {"secretName": "comm-sa-key"}},
                            {"name": "tmp-vol", "emptyDir": {}},
                        ],
                    },
                },
            },
        }

        # Add TTL if specified
        if ttl_seconds_after_finished is not None:
            job_manifest["spec"]["ttlSecondsAfterFinished"] = ttl_seconds_after_finished

        # Create the job
        try:
            api_response = batch_api.create_namespaced_job(
                namespace=namespace,
                body=job_manifest,
            )

            print(f"✅ Job created successfully!")
            print(f"   Job name: {api_response.metadata.name}")
            print(f"   Job UID: {api_response.metadata.uid}")
            print(f"   Namespace: {namespace}")
            print(f"   Image: {image}")

            return api_response

        except ApiException as e:
            if e.status == 409:  # Conflict - job already exists
                print(f"⚠️  Job already exists: {job_name}")
                return None
            else:
                raise e

    except Exception as e:
        print(f"❌ Error creating job: {e}")
        return None


def get_job_logs(
    core_api,
    job_name: str,
    namespace: str = "default",
    tail_lines: int = 10,
):
    """
    Get logs from pods associated with a Kubernetes job.

    Args:
        core_api: Kubernetes CoreV1Api client
        job_name: Name of the job
        namespace: Kubernetes namespace
        tail_lines: Number of lines to tail (default: 10)
    """
    try:
        # Find pods associated with the job
        pods = core_api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"job-name={job_name}",
        )

        if not pods.items:
            return {
                "success": False,
                "message": f"No pods found for job: {job_name}",
                "job_name": job_name,
                "namespace": namespace,
                "logs": [],
            }

        # Get logs from each pod
        pod = pods.items[0]
        pod_name = pod.metadata.name
        pod_status = pod.status.phase

        # Count ready containers properly
        ready_containers = 0
        if pod.status.container_statuses:
            ready_containers = sum(
                1 for container in pod.status.container_statuses if container.ready
            )

        pod_data = {
            "pod_name": pod_name,
            "status": pod_status,
            "ready_containers": ready_containers,
            "total_containers": len(pod.spec.containers),
        }

        # Get logs from the pod
        logs = core_api.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
        )

        if logs:
            pod_data["logs"] = logs
        else:
            pod_data["logs"] = "(no logs available)\n"

        return {
            "success": True,
            "message": f"Retrieved logs for job: {job_name}",
            "job_name": job_name,
            "namespace": namespace,
            "tail_lines": tail_lines,
            "logs": pod_data["logs"].split("\n"),
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"Error accessing job {job_name}: {str(e)}",
            "job_name": job_name,
            "namespace": namespace,
            "logs": [],
        }


def suspend_job(batch_api, job_name: str, namespace: str = "default"):
    """Suspend a GKE Job by preventing new pods and deleting existing ones without
    deleting the Job resource."""
    try:
        patch_body = {"spec": {"suspend": True}}
        batch_api.patch_namespaced_job(
            name=job_name,
            namespace=namespace,
            body=patch_body,
        )
        print(f"Patched job to suspend: {job_name}")
        return True
    except Exception as e:
        print(f"Error stopping job: {e}")
        return False


# ---------------------------------------------------------------------------
# K8s Lease-based distributed lock for atomic container assignment
# ---------------------------------------------------------------------------

LEASE_DURATION_SECONDS = 60


def _sanitize_for_k8s(value: str) -> str:
    """Sanitize a value for use in K8s resource names and labels."""
    return str(value).lower().replace("_", "-")


def acquire_assignment_lease(
    coord_api,
    assistant_id: str,
    namespace: str,
    holder_id: str,
    duration: int = LEASE_DURATION_SECONDS,
) -> bool:
    """Atomically acquire a Lease for assigning a container to an assistant.

    Returns True if the Lease was acquired, False if another caller holds it.
    Stale Leases (older than *duration* seconds) are cleaned up automatically.
    """
    lease_name = f"assistant-claim-{_sanitize_for_k8s(assistant_id)}"
    now = datetime.now(timezone.utc)

    lease_body = k8s_client.V1Lease(
        metadata=k8s_client.V1ObjectMeta(name=lease_name, namespace=namespace),
        spec=k8s_client.V1LeaseSpec(
            holder_identity=holder_id,
            lease_duration_seconds=duration,
            acquire_time=now,
            renew_time=now,
        ),
    )

    try:
        coord_api.create_namespaced_lease(namespace=namespace, body=lease_body)
        logger.info("Acquired assignment lease %s (holder=%s)", lease_name, holder_id)
        return True
    except ApiException as e:
        if e.status != 409:
            raise

    existing = coord_api.read_namespaced_lease(name=lease_name, namespace=namespace)
    acquire_time = existing.spec.acquire_time
    lease_dur = existing.spec.lease_duration_seconds or duration

    if acquire_time and (now - acquire_time).total_seconds() > lease_dur:
        logger.info("Deleting expired lease %s (age > %ds)", lease_name, lease_dur)
        coord_api.delete_namespaced_lease(name=lease_name, namespace=namespace)
        try:
            coord_api.create_namespaced_lease(namespace=namespace, body=lease_body)
            logger.info("Re-acquired expired lease %s", lease_name)
            return True
        except ApiException as retry_err:
            if retry_err.status == 409:
                return False
            raise

    logger.info(
        "Lease %s held by %s, not expired",
        lease_name,
        existing.spec.holder_identity,
    )
    return False


def release_assignment_lease(
    coord_api,
    assistant_id: str,
    namespace: str,
) -> None:
    """Delete the assignment Lease. Ignores 404 (already released)."""
    lease_name = f"assistant-claim-{_sanitize_for_k8s(assistant_id)}"
    try:
        coord_api.delete_namespaced_lease(name=lease_name, namespace=namespace)
        logger.info("Released assignment lease %s", lease_name)
    except ApiException as e:
        if e.status != 404:
            raise


def claim_idle_container(
    batch_api,
    assistant_id: str,
    namespace: str,
    startup_config_json: str,
) -> str:
    """Pick an idle container and atomically assign it to *assistant_id*.

    Writes the startup configuration as a Job annotation so the container
    can read it via polling.  Uses resourceVersion CAS on the label patch
    to prevent two different assistants from claiming the same idle Job.

    Returns the claimed Job's name, or raises if no idle containers are
    available.
    """
    sanitized_aid = _sanitize_for_k8s(assistant_id)

    idle_jobs = batch_api.list_namespaced_job(
        namespace=namespace,
        label_selector="app=unity,unity-status=idle",
    )
    candidates = [j for j in idle_jobs.items if j.status.active and j.status.active > 0]

    if not candidates:
        raise RuntimeError("No idle containers available in the pool")

    for job in candidates:
        job_name = job.metadata.name
        rv = job.metadata.resource_version
        labels = dict(job.metadata.labels or {})
        labels["assistant-id"] = sanitized_aid
        labels["unity-status"] = "running"

        body = {
            "metadata": {
                "labels": labels,
                "annotations": {"unity-startup-config": startup_config_json},
                "resourceVersion": rv,
            },
        }

        try:
            batch_api.patch_namespaced_job(
                name=job_name,
                namespace=namespace,
                body=body,
            )
            logger.info(
                "Claimed container %s for assistant %s (rv=%s)",
                job_name,
                assistant_id,
                rv,
            )
            return job_name
        except ApiException as e:
            if e.status == 409:
                logger.info(
                    "CAS conflict on %s (rv=%s), trying next idle container",
                    job_name,
                    rv,
                )
                continue
            raise

    raise RuntimeError(
        "All idle containers were claimed by concurrent requests; retry later",
    )


# ---------------------------------------------------------------------------
# Pending-startup reconciliation (Pub/Sub consumer)
# ---------------------------------------------------------------------------

GCP_PROJECT_ID = "gcp-project-runtime"
_STAGING = os.environ.get("STAGING", "false").lower() == "true"
_PENDING_TOPIC = "unity-pending-startups" + ("-staging" if _STAGING else "")
_PENDING_SUB = _PENDING_TOPIC + "-sub"


def publish_pending_startup(startup_config_json: str) -> str:
    """Publish a startup config to the pending-startups topic.

    Called by /infra/job/start when no idle container is available.
    Returns the Pub/Sub message ID.
    """
    from google.cloud import pubsub_v1
    from google.oauth2.service_account import Credentials

    creds_json = json.loads(os.getenv("GCP_SA_KEY", "{}"))
    creds = Credentials.from_service_account_info(creds_json)
    publisher = pubsub_v1.PublisherClient(credentials=creds)
    topic_path = publisher.topic_path(GCP_PROJECT_ID, _PENDING_TOPIC)
    future = publisher.publish(topic_path, data=startup_config_json.encode("utf-8"))
    message_id = future.result()
    logger.info(
        "Published pending startup to %s (msg_id=%s)",
        _PENDING_TOPIC,
        message_id,
    )
    return message_id


def process_pending_startups(
    batch_api,
    coord_api,
    namespace: str,
) -> dict:
    """Pull pending startup messages and assign them to idle containers.

    Stateless and idempotent — safe to call from any trigger at any
    frequency.  Each message is independently acked (assistant already
    served or successfully claimed) or nacked (no idle container or
    Lease contention — retried on next trigger).
    """
    from google.cloud import pubsub_v1
    from google.oauth2.service_account import Credentials

    from google.api_core.exceptions import DeadlineExceeded

    creds_json = json.loads(os.getenv("GCP_SA_KEY", "{}"))
    creds = Credentials.from_service_account_info(creds_json)
    subscriber = pubsub_v1.SubscriberClient(credentials=creds)
    sub_path = subscriber.subscription_path(GCP_PROJECT_ID, _PENDING_SUB)

    try:
        response = subscriber.pull(
            request={"subscription": sub_path, "max_messages": 10},
            timeout=10,
        )
        messages = response.received_messages
    except DeadlineExceeded:
        messages = []

    if not messages:
        return {"pulled": 0, "acked": 0, "nacked": 0}

    logger.info("Pulled %d pending startup message(s)", len(messages))

    acked = 0
    nacked = 0

    for msg in messages:
        ack_id = msg.ack_id
        try:
            config = json.loads(msg.message.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Malformed pending startup message, acking to discard")
            subscriber.acknowledge(
                request={"subscription": sub_path, "ack_ids": [ack_id]},
            )
            acked += 1
            continue

        assistant_id = str(config.get("assistant_id", ""))
        if not assistant_id:
            subscriber.acknowledge(
                request={"subscription": sub_path, "ack_ids": [ack_id]},
            )
            acked += 1
            continue

        sanitized_aid = _sanitize_for_k8s(assistant_id)

        existing = batch_api.list_namespaced_job(
            namespace=namespace,
            label_selector=f"app=unity,assistant-id={sanitized_aid}",
        )
        already_running = [
            j for j in existing.items if j.status.active and j.status.active > 0
        ]
        if already_running:
            logger.info(
                "Assistant %s already has container %s, acking pending message",
                assistant_id,
                already_running[0].metadata.name,
            )
            subscriber.acknowledge(
                request={"subscription": sub_path, "ack_ids": [ack_id]},
            )
            acked += 1
            continue

        holder_id = f"reconcile-{uuid.uuid4().hex[:8]}"
        acquired = acquire_assignment_lease(
            coord_api,
            assistant_id,
            namespace,
            holder_id,
        )
        if not acquired:
            logger.info("Lease held for assistant %s, nacking for retry", assistant_id)
            subscriber.modify_ack_deadline(
                request={
                    "subscription": sub_path,
                    "ack_ids": [ack_id],
                    "ack_deadline_seconds": 0,
                },
            )
            nacked += 1
            continue

        try:
            startup_config_json = json.dumps(config)
            job_name = claim_idle_container(
                batch_api,
                assistant_id,
                namespace,
                startup_config_json,
            )
            logger.info(
                "Reconciler assigned container %s to assistant %s",
                job_name,
                assistant_id,
            )
            subscriber.acknowledge(
                request={"subscription": sub_path, "ack_ids": [ack_id]},
            )
            acked += 1
        except RuntimeError:
            logger.info(
                "No idle container for assistant %s, nacking for retry",
                assistant_id,
            )
            subscriber.modify_ack_deadline(
                request={
                    "subscription": sub_path,
                    "ack_ids": [ack_id],
                    "ack_deadline_seconds": 0,
                },
            )
            nacked += 1
        finally:
            release_assignment_lease(coord_api, assistant_id, namespace)

    return {"pulled": len(messages), "acked": acked, "nacked": nacked}
