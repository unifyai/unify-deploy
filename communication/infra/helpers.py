import json
import os
import subprocess
import threading
from kubernetes import client as k8s_client, config
from kubernetes.client.rest import ApiException

from communication.helpers import ADAPTERS_URL, COMMS_URL, ORCHESTRA_URL

_k8s_clients: tuple | None = None
_k8s_lock = threading.Lock()


def setup_kubernetes_client():
    """Initialize Kubernetes client using GKE authentication.

    The clients are cached after the first successful setup so that subsequent
    calls return instantly instead of re-running gcloud auth subprocesses.

    Returns:
        tuple: (BatchV1Api, CoreV1Api, NetworkingV1Api) - Kubernetes API clients
        tuple: (None, None, None) - If setup fails
    """
    global _k8s_clients

    if _k8s_clients is not None:
        return _k8s_clients

    with _k8s_lock:
        # Double-check after acquiring lock
        if _k8s_clients is not None:
            return _k8s_clients

        try:
            print("🔧 Starting Kubernetes client setup...")

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
                "✅ Successfully authenticated with gcloud and got cluster credentials",
            )

            print("⚙️  Loading kubeconfig...")
            config.load_kube_config()

            print("🔗 Creating API clients...")
            batch_api = k8s_client.BatchV1Api()
            core_api = k8s_client.CoreV1Api()
            networking_api = k8s_client.NetworkingV1Api()

            print("🧪 Testing API connection...")
            namespaces = core_api.list_namespace(limit=1)
            print(
                f"✅ Successfully connected! Found {len(namespaces.items)} namespaces",
            )

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
    required_labels: dict | None = None,
):
    """Delete a Unity job.

    Args:
        required_labels: If provided, the job's current labels must contain all
            of these key-value pairs or the deletion is skipped (returns False).
            Guards against TOCTOU races where a job's status changes between
            listing and deletion.
    """
    try:
        if required_labels:
            job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
            current_labels = job.metadata.labels or {}
            print(f"Current labels: {current_labels}")
            print(f"Required labels: {required_labels}")
            for key, value in required_labels.items():
                if current_labels.get(key) != value:
                    print(
                        f"⏭️  Skipping delete for {job_name}: "
                        f"label {key}={current_labels.get(key)!r}, expected {value!r}",
                    )
                    return False

        batch_api.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            propagation_policy="Background",
        )

        print(f"✅ Job deleted successfully: {job_name}")
        return True

    except ApiException as e:
        if e.status == 404:
            print(f"⚠️  Job not found (already deleted): {job_name}")
            return True
        else:
            print(f"❌ Error deleting job: {e}")
            return False


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
                        "priorityClassName": "unity-critical",
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
