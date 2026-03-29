from datetime import datetime, timezone
import base64
import json
import os
import tempfile
import threading
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException
from google.oauth2 import service_account as google_sa
import google.auth.transport.requests
from googleapiclient.discovery import build as _build_gke_svc

from communication.helpers import ADAPTERS_URL, COMMS_URL, DEPLOY_ENV, ORCHESTRA_URL

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
        tuple: (BatchV1Api, CoreV1Api, NetworkingV1Api) - Kubernetes API clients
        tuple: (None, None, None) - If setup fails
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
                print("❌ GCP_SA_KEY environment variable not set")
                return None, None, None

            creds_data = json.loads(creds_json)
            project_id = creds_data.get("project_id", "gcp-project-runtime")
            cluster_name = "unity"
            region = "us-central1"

            print(
                f"🔑 Using service account: {creds_data.get('client_email', 'unknown')}",
            )

            # Build scoped credentials and fetch an initial token
            _gke_credentials = google_sa.Credentials.from_service_account_info(
                creds_data,
                scopes=_GKE_SCOPES,
            )
            auth_request = google.auth.transport.requests.Request()
            _gke_credentials.refresh(auth_request)

            # Fetch cluster endpoint and CA cert via the GKE REST API
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

            # Write CA cert to a temp file for the K8s client
            ca_file = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            ca_file.write(base64.b64decode(ca_cert_b64))
            ca_file.close()
            _ca_cert_path = ca_file.name

            # Build K8s Configuration
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

            print("✅ Kubernetes client setup complete!")

            _k8s_clients = (batch_api, core_api, networking_api)
            return _k8s_clients

        except Exception as e:
            print(f"❌ Error setting up Kubernetes client: {e}")
            return None, None, None


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
    ttl_seconds_after_finished: int = None,
):
    """
    Create a Kubernetes Job for a Unity assistant.

    Args:
        batch_api: Kubernetes Batch API client
        job_name: Name of the job
        namespace: Kubernetes namespace
        image: Docker image to use
        ttl_seconds_after_finished: Seconds after job completion before cleanup (None to disable)
    """
    try:
        # Define the assistant-specific environment variables
        env_vars = [
            {"name": "UNITY_CONVERSATION_JOB_NAME", "value": job_name},
            {"name": "DEPLOY_ENV", "value": DEPLOY_ENV},
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
        print(f"🛑 Patched job to stop new pods and retries: {job_name}")
        return True
    except Exception as e:
        print(f"❌ Error stopping job: {e}")
        return False
