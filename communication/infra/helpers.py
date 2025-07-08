import json
import os
from kubernetes import client, config
from kubernetes.client.rest import ApiException


def setup_kubernetes_client():
    """Initialize Kubernetes client using service account authentication"""
    try:
        # Set the service account credentials as environment variable
        creds_json = os.getenv("GCP_SA_KEY")
        if not creds_json:
            print("❌ GCP_SA_KEY environment variable not set")
            return None, None

        # Parse credentials to get project ID
        creds_data = json.loads(creds_json)
        project_id = creds_data.get("project_id", "gcp-project-runtime")

        # Set up cluster configuration
        cluster_name = "unity"
        region = "us-central1"

        # Try in-cluster config first (if running inside Kubernetes)
        try:
            print("🔗 Attempting to use in-cluster Kubernetes config...")
            config.load_incluster_config()
            print("✅ Successfully loaded in-cluster config")
        except Exception as incluster_error:
            print(f"⚠️  In-cluster config failed: {incluster_error}")
            print("🔄 Falling back to kubeconfig...")

            # Fallback: try to use default kubeconfig
            try:
                config.load_kube_config()
                print("✅ Successfully loaded kubeconfig")
            except Exception as kubeconfig_error:
                print(f"❌ Kubeconfig also failed: {kubeconfig_error}")
                print("💡 Make sure you have:")
                print("   1. Valid kubeconfig file (~/.kube/config)")
                print(
                    "   2. Or run 'gcloud container clusters get-credentials unity --region us-central1'"
                )
                return None, None

        return client.BatchV1Api(), client.CoreV1Api()

    except Exception as e:
        print(f"❌ Error setting up Kubernetes client: {e}")
        return None, None


def check_job_exists(batch_api, assistant_id: str, namespace: str = "default"):
    """Check if a job for this assistant already exists and is running"""
    try:
        job_name = f"unity-{assistant_id}"
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


def delete_job(batch_api, assistant_id: str, namespace: str = "default"):
    """Delete a Unity job"""
    try:
        job_name = f"unity-{assistant_id}"

        api_response = batch_api.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            propagation_policy="Background",  # Delete pods as well
        )

        print(f"✅ Job deleted successfully: {job_name}")
        return True

    except ApiException as e:
        if e.status == 404:
            print(f"⚠️  Job not found (already deleted): unity-{assistant_id}")
            return True
        else:
            print(f"❌ Error deleting job: {e}")
            return False


def create_unity_job(
    batch_api,
    api_key: str,
    assistant_id: str,
    user_name: str,
    user_number: str,
    assistant_number: str = "",
    user_phone_number: str = "",
    namespace: str = "default",
    image: str = "us-central1-docker.pkg.dev/gcp-project-runtime/unity/unity:latest",
):
    """
    Create a Kubernetes Job for a Unity assistant.

    Args:
        batch_api: Kubernetes Batch API client
        api_key: API key for authentication
        assistant_id: Unique assistant identifier
        user_name: User's name
        user_number: User's phone number
        assistant_number: Assistant's phone number (optional)
        user_phone_number: User's phone for calls (defaults to user_number)
        namespace: Kubernetes namespace
        image: Docker image to use
    """
    try:
        # Check if job already exists and is running
        exists, status = check_job_exists(batch_api, assistant_id, namespace)

        if exists and status == "running":
            print(f"✅ Assistant {assistant_id} is already running")
            return None
        elif exists and status in ["completed", "failed"]:
            print(f"🗑️  Cleaning up old job for {assistant_id} (status: {status})")
            delete_job(batch_api, assistant_id, namespace)

        # Create the job name with unity- prefix
        job_name = f"unity-{assistant_id}"

        # Define the job manifest
        job_manifest = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
                "labels": {
                    "app": "unity",
                    "assistant-id": assistant_id,
                    "created-by": "create_job_script",
                },
            },
            "spec": {
                "backoffLimit": 1,  # Allow 1 retry for resource issues
                "activeDeadlineSeconds": 7200,  # 2 hours max runtime
                "ttlSecondsAfterFinished": 0,  # Auto-delete job and pods after specified delay
                "template": {
                    "metadata": {
                        "labels": {"app": "unity", "assistant-id": assistant_id}
                    },
                    "spec": {
                        "restartPolicy": "Never",
                        "serviceAccountName": "comm-sa",
                        "terminationGracePeriodSeconds": 30,  # Faster termination
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
                                "env": [
                                    # Assistant-specific environment variables
                                    {"name": "ASSISTANT_ID", "value": assistant_id},
                                    {"name": "USER_NAME", "value": user_name},
                                    {
                                        "name": "ASSISTANT_NUMBER",
                                        "value": assistant_number,
                                    },
                                    {"name": "USER_NUMBER", "value": user_number},
                                    {
                                        "name": "USER_PHONE_NUMBER",
                                        "value": user_phone_number or user_number,
                                    },
                                    {"name": "UNIFY_KEY", "value": api_key},
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
                                ],
                                "resources": {
                                    "requests": {"cpu": "2", "memory": "8Gi"},
                                    "limits": {"cpu": "4", "memory": "16Gi"},
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

        # Create the job
        try:
            api_response = batch_api.create_namespaced_job(
                namespace=namespace, body=job_manifest
            )

            print(f"✅ Job created successfully!")
            print(f"   Job name: {api_response.metadata.name}")
            print(f"   Job UID: {api_response.metadata.uid}")
            print(f"   Assistant ID: {assistant_id}")
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
