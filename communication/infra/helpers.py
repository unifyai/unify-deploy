import json
import os
import subprocess
from kubernetes import client as k8s_client, config
from kubernetes.client.rest import ApiException


def setup_kubernetes_client():
    """Initialize Kubernetes client using GKE authentication

    Returns:
        tuple: (BatchV1Api, CoreV1Api) - Kubernetes API clients for batch operations and core operations
        tuple: (None, None) - If setup fails
    """
    try:
        print("🔧 Starting Kubernetes client setup...")

        # Get service account credentials from environment variable
        creds_json = os.getenv("GCP_SA_KEY")
        if not creds_json:
            print("❌ GCP_SA_KEY environment variable not set")
            return None, None

        # Parse credentials to get project info
        creds_data = json.loads(creds_json)
        project_id = creds_data.get("project_id", "gcp-project-runtime")
        service_account_email = creds_data.get("client_email", "unknown")

        print(f"🔑 Using service account: {service_account_email}")
        print(f"🏗️  Project: {project_id}")

        # Cluster configuration
        cluster_name = "unity"
        region = "us-central1"

        print("🔐 Setting up GKE authentication...")
        # Write service account key to file for gcloud authentication
        sa_key_path = "/tmp/gcp-sa-key.json"
        with open(sa_key_path, "w") as f:
            f.write(creds_json)

        # Set GOOGLE_APPLICATION_CREDENTIALS
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_key_path

        # Authenticate with gcloud using service account
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

        # Get cluster credentials using gcloud
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

        print("✅ Successfully authenticated with gcloud and got cluster credentials")

        # Load kubeconfig and create API clients
        print("⚙️  Loading kubeconfig...")
        config.load_kube_config()

        print("🔗 Creating API clients...")
        batch_api = k8s_client.BatchV1Api()
        core_api = k8s_client.CoreV1Api()

        # Test the connection
        print("🧪 Testing API connection...")
        namespaces = core_api.list_namespace(limit=1)
        print(f"✅ Successfully connected! Found {len(namespaces.items)} namespaces")

        print("✅ Kubernetes client setup complete!")

        return batch_api, core_api

    except Exception as e:
        print(f"❌ Error setting up Kubernetes client: {e}")
        return None, None


def check_job_exists(batch_api, job_name: str, namespace: str = "default"):
    """Check if a job for this assistant already exists and is running"""
    try:
        job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)

        # Check if job is active (has running pods)
        if job.status.active and job.status.active > 0:
            return True, "running"
        elif job.status.succeeded and job.status.succeeded > 0:
            return True, "completed"
        elif job.status.failed and job.status.failed > 0:
            return True, "failed"
        else:
            return True, "unknown"

    except ApiException as e:
        if e.status == 404:
            return False, None
        else:
            raise e


def delete_job(batch_api, job_name: str, namespace: str = "default"):
    """Delete a Unity job"""
    try:
        batch_api.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            propagation_policy="Background",  # Delete pods as well
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


def create_unity_job(
    batch_api,
    job_name: str,
    namespace: str = "default",
    image: str = "us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity:latest",
    is_staging: bool = False,
    ttl_seconds_after_finished: int = 86400,  # 24 hours default, set to None to disable
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
        ]
        if is_staging:
            env_vars = [
                {"name": "STAGING", "value": "true"},
                {
                    "name": "UNIFY_BASE_URL",
                    "value": "https://service.a.run.app/v0"
                }
            ] + env_vars

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
                },
            },
            "spec": {
                "backoffLimit": 1,  # Allow 1 retry for resource issues
                "template": {
                    "metadata": {"labels": {"app": "unity"}},
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": "comm-sa",
                        "terminationGracePeriodSeconds": 30,  # Faster termination
                        "activeDeadlineSeconds": 86400,  # 24 hours
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
                                    "requests": {"cpu": "2", "memory": "8Gi"},
                                    "limits": {"cpu": "2", "memory": "8Gi"},
                                },
                                "volumeMounts": [
                                    {
                                        "name": "sa-key",
                                        "mountPath": "/secrets",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "sa-key", "secret": {"secretName": "comm-sa-key"}}
                        ],
                    },
                },
            },
        }

        # Add TTL if specified
        if ttl_seconds_after_finished is not None:
            job_manifest["spec"]["ttlSecondsAfterFinished"] = (
                ttl_seconds_after_finished
            )

        # Create the job
        try:
            api_response = batch_api.create_namespaced_job(
                namespace=namespace, body=job_manifest
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
    core_api, job_name: str, namespace: str = "default", tail_lines: int = 10
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
