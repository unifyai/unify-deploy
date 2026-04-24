from datetime import datetime, timezone
import base64
import json
import logging
import os
import tempfile
import threading
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from google.oauth2 import service_account as google_sa
import google.auth.transport.requests
from googleapiclient.discovery import build as _build_gke_svc

from common.settings import SETTINGS

logger = logging.getLogger(__name__)

_k8s_clients: tuple | None = None
_k8s_lock = threading.Lock()

_GKE_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_gke_credentials: google_sa.Credentials | None = None
_ca_cert_path: str | None = None


class _GKEApiClient(k8s_client.ApiClient):
    """ApiClient subclass that auto-refreshes the GKE access token."""

    def __init__(self, configuration, credentials):
        super().__init__(configuration)
        self._gke_creds = credentials
        self._auth_request = google.auth.transport.requests.Request()
        self._refresh_lock = threading.Lock()

    def call_api(self, *args, **kwargs):
        if not self._gke_creds.valid:
            with self._refresh_lock:
                if not self._gke_creds.valid:
                    self._gke_creds.refresh(self._auth_request)
                    self.configuration.api_key["authorization"] = self._gke_creds.token
        return super().call_api(*args, **kwargs)


def setup_kubernetes_client():
    """Initialize Kubernetes client using programmatic GKE authentication.

    Uses ``google.oauth2.service_account`` and the GKE REST API to
    configure the K8s client directly — no ``gcloud`` CLI subprocess
    needed.  Tokens are auto-refreshed before each API call via a
    custom ``ApiClient`` subclass.

    Returns:
        tuple: (BatchV1Api, CoreV1Api, NetworkingV1Api, CoordinationV1Api)
        tuple: (None, None, None, None) - If setup fails
    """
    global _k8s_clients, _gke_credentials, _ca_cert_path

    if _k8s_clients is not None:
        return _k8s_clients

    with _k8s_lock:
        if _k8s_clients is not None:
            return _k8s_clients

        try:
            print("🔧 Starting Kubernetes client setup (programmatic)...")

            creds_json = os.getenv("GCP_SA_KEY")
            if not creds_json:
                try:
                    k8s_config.load_incluster_config()
                    batch_api = k8s_client.BatchV1Api()
                    core_api = k8s_client.CoreV1Api()
                    networking_api = k8s_client.NetworkingV1Api()
                    coord_api = k8s_client.CoordinationV1Api()
                    _k8s_clients = (batch_api, core_api, networking_api, coord_api)
                    print("✅ Kubernetes client setup complete (in-cluster)!")
                    return _k8s_clients
                except Exception:
                    print("❌ GCP_SA_KEY environment variable not set")
                    return None, None, None, None

            creds_data = json.loads(creds_json)
            project_id = creds_data.get("project_id", SETTINGS.gcp_project_id)
            cluster_name = SETTINGS.gke_cluster_name
            region = SETTINGS.default_region

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
            batch_api = k8s_client.BatchV1Api(api_client)
            core_api = k8s_client.CoreV1Api(api_client)
            networking_api = k8s_client.NetworkingV1Api(api_client)
            coord_api = k8s_client.CoordinationV1Api(api_client)

            print("✅ Kubernetes client setup complete!")

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
    """Patch labels on an existing Unity job.

    Includes a server-side guard: if the job already has an assistant-id
    assigned and the patch tries to set unity-status back to idle, the
    patch is rejected.  This prevents a delayed boot-time "I'm idle"
    label from overwriting a valid container assignment.
    """
    try:
        if labels.get("unity-status") == "idle":
            job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
            current_labels = job.metadata.labels or {}
            if current_labels.get("assistant-id"):
                logger.warning(
                    "Rejected idle label patch on %s: assistant-id=%s already set",
                    job_name,
                    current_labels.get("assistant-id"),
                )
                return True

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
    image: str = f"{SETTINGS.image_registry}/{SETTINGS.unity_image_name}:latest",
    deploy_env: str = SETTINGS.deploy_env,
    ttl_seconds_after_finished: int = None,
    active_deadline_seconds: int | None = None,
    unity_status: str = "idle",
    priority_class_name: str | None = None,
    app_label: str = "unity",
    extra_labels: dict | None = None,
    extra_annotations: dict | None = None,
    extra_env: dict[str, str] | None = None,
):
    """
    Create a Kubernetes Job for a Unity assistant.

    Args:
        batch_api: Kubernetes Batch API client
        job_name: Name of the job
        namespace: Kubernetes namespace
        image: Docker image to use
        deploy_env: Deployment environment ("production" or "staging")
        ttl_seconds_after_finished: Seconds after job completion before cleanup (None to disable)
    """
    try:
        env_vars = [
            {"name": "UNITY_CONVERSATION_JOB_NAME", "value": job_name},
            {"name": "DEPLOY_ENV", "value": deploy_env},
            {
                "name": "GOOGLE_APPLICATION_CREDENTIALS",
                "value": "/secrets/key.json",
            },
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
            {"name": "UNITY_COMMS_URL", "value": SETTINGS.comms_url},
            {"name": "UNITY_ADAPTERS_URL", "value": SETTINGS.adapters_url},
            {"name": "ORCHESTRA_URL", "value": SETTINGS.orchestra_url},
            # Pipeline worker dispatch: route attachment ingestion through the
            # GKE parse/ingest workers via Pub/Sub (topic names are derived
            # from GCP_PROJECT_ID + DEPLOY_ENV, matching the existing
            # ``unity-{name}{env_suffix}`` convention).
            {"name": "UNITY_FILE_PIPELINE_DISPATCH_ENABLED", "value": "false"},
            {
                "name": "UNITY_FILE_PIPELINE_ARTIFACT_BUCKET",
                "value": (
                    "unity-pipeline-artifacts"
                    if deploy_env == "production"
                    else f"unity-pipeline-artifacts-{deploy_env}"
                ),
            },
        ]
        if deploy_env == "staging":
            env_vars += [{"name": "STAGING", "value": "true"}]
        if extra_env:
            env_vars.extend(
                {"name": str(key), "value": str(value)}
                for key, value in extra_env.items()
            )

        metadata_labels = {
            "app": app_label,
            "created-by": "create_job_script",
            "unity-status": unity_status,
            "unity-date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "unity-image-hash": (
                image.rsplit(":", 1)[-1] if ":" in image else "unknown"
            ),
        }
        if extra_labels:
            metadata_labels.update(extra_labels)

        pod_annotations = {
            "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
        }
        if extra_annotations:
            pod_annotations.update(extra_annotations)

        metadata_annotations = dict(extra_annotations or {})

        # Define the job manifest
        job_manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
                "labels": metadata_labels,
                "annotations": metadata_annotations,
            },
            "spec": {
                "backoffLimit": 0,
                "template": {
                    "metadata": {
                        "labels": {"app": app_label},
                        "annotations": pod_annotations,
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": "comm-sa",
                        "terminationGracePeriodSeconds": 30,  # Faster termination
                        "priorityClassName": (priority_class_name or "unity-idle"),
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
                                # Right-sized 2026-04 from 2 vCPU / 16 GiB based on
                                # 30 days of per-pod metrics: p999 CPU = 0.25 cores,
                                # p999 memory = 2.5 GiB, max-ever memory = 5.6 GiB
                                # in production. 2 vCPU keeps ~8x headroom over p999
                                # CPU; 8 GiB keeps ~3x headroom over p999 memory and
                                # ~40% over the 30d max. Note: previous "2 vCPU /
                                # 16 GiB" was actually billed as 2.46 vCPU because
                                # Autopilot's 1:6.5 vCPU:memory ratio bumps CPU up
                                # at 16 GiB. At 8 GiB the requested 2 vCPU is
                                # honoured as-is, so this also drops effective CPU.
                                "resources": {
                                    "requests": {
                                        "cpu": "2",
                                        "memory": "8Gi",
                                        "ephemeral-storage": "10Gi",
                                    },
                                    "limits": {
                                        "cpu": "2",
                                        "memory": "8Gi",
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
        if active_deadline_seconds is not None:
            job_manifest["spec"]["activeDeadlineSeconds"] = active_deadline_seconds

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


def _sanitize_for_k8s(value: str) -> str:
    """Sanitize a value for use in K8s resource names and labels."""
    return str(value).lower().replace("_", "-")


def _lease_name(prefix: str, lease_id: str) -> str:
    """Build a stable Kubernetes Lease name for a logical lock scope."""
    return f"{prefix}-{_sanitize_for_k8s(lease_id)}"


def acquire_named_lease(
    coord_api,
    lease_name: str,
    namespace: str,
    holder_id: str,
    duration: int = SETTINGS.lease_duration_seconds,
) -> bool:
    """Atomically acquire a named Lease in Kubernetes.

    Returns True if the Lease was acquired, False if another caller holds it.
    Stale Leases (older than *duration* seconds) are cleaned up automatically.
    """
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
        logger.info("Acquired lease %s (holder=%s)", lease_name, holder_id)
        return True
    except ApiException as e:
        if e.status != 409:
            raise

    try:
        existing = coord_api.read_namespaced_lease(name=lease_name, namespace=namespace)
    except ApiException as read_err:
        if read_err.status != 404:
            raise
        logger.info(
            "Lease %s disappeared after create conflict; retrying acquisition",
            lease_name,
        )
        try:
            coord_api.create_namespaced_lease(namespace=namespace, body=lease_body)
            logger.info(
                "Acquired lease %s (holder=%s) after conflict handoff",
                lease_name,
                holder_id,
            )
            return True
        except ApiException as retry_err:
            if retry_err.status == 409:
                return False
            raise

    acquire_time = existing.spec.acquire_time
    lease_dur = existing.spec.lease_duration_seconds or duration

    if acquire_time and (now - acquire_time).total_seconds() > lease_dur:
        logger.info("Deleting expired lease %s (age > %ds)", lease_name, lease_dur)
        if not _delete_observed_lease(
            coord_api,
            existing,
            reason="expired_lease_cleanup",
        ):
            return False
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


def _delete_observed_lease(coord_api, lease, *, reason: str) -> bool:
    """Delete the exact Lease object that was previously observed."""

    metadata = getattr(lease, "metadata", None)
    lease_name = str(getattr(metadata, "name", "") or "")
    namespace = str(getattr(metadata, "namespace", "") or "")
    lease_uid = str(getattr(metadata, "uid", "") or "")
    delete_options = None
    if lease_uid:
        delete_options = k8s_client.V1DeleteOptions(
            preconditions=k8s_client.V1Preconditions(uid=lease_uid),
        )

    try:
        if delete_options is None:
            coord_api.delete_namespaced_lease(name=lease_name, namespace=namespace)
        else:
            coord_api.delete_namespaced_lease(
                name=lease_name,
                namespace=namespace,
                body=delete_options,
            )
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        if e.status in {409, 422}:
            logger.info(
                "Skipped deleting lease %s during %s because the observed "
                "instance changed",
                lease_name,
                reason,
            )
            return False
        raise


def acquire_assignment_lease(
    coord_api,
    assistant_id: str,
    namespace: str,
    holder_id: str,
    duration: int = SETTINGS.lease_duration_seconds,
) -> bool:
    """Atomically acquire a Lease for assigning a container to an assistant.

    Returns True if the Lease was acquired, False if another caller holds it.
    Stale Leases (older than *duration* seconds) are cleaned up automatically.
    """
    return acquire_named_lease(
        coord_api=coord_api,
        lease_name=_lease_name("assistant-claim", assistant_id),
        namespace=namespace,
        holder_id=holder_id,
        duration=duration,
    )


def release_named_lease(
    coord_api,
    lease_name: str,
    namespace: str,
    holder_id: str,
) -> None:
    """Delete a named Lease only if it is still held by ``holder_id``."""

    try:
        existing = coord_api.read_namespaced_lease(name=lease_name, namespace=namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        return

    current_holder_id = str(existing.spec.holder_identity or "")
    if current_holder_id != holder_id:
        logger.info(
            "Skipped releasing lease %s because holder changed from %s to %s",
            lease_name,
            holder_id,
            current_holder_id or "<none>",
        )
        return

    if _delete_observed_lease(coord_api, existing, reason="lease_release"):
        logger.info("Released lease %s (holder=%s)", lease_name, holder_id)


def release_assignment_lease(
    coord_api,
    assistant_id: str,
    namespace: str,
    holder_id: str,
) -> None:
    """Delete the assignment Lease only if ``holder_id`` still owns it."""
    release_named_lease(
        coord_api=coord_api,
        lease_name=_lease_name("assistant-claim", assistant_id),
        namespace=namespace,
        holder_id=holder_id,
    )
