import json
import os
import subprocess
from kubernetes import client as k8s_client, config
from kubernetes.client.rest import ApiException
from communication.helpers import STAGING

# Ingress configuration for HTTPS
INGRESS_NAME = "desktop-unity-ingress"
INGRESS_NAMESPACE = "staging" if STAGING else "production"
DESKTOP_DOMAIN = "staging.desktop.unify.ai" if STAGING else "desktop.unify.ai"


def setup_kubernetes_client():
    """Initialize Kubernetes client using GKE authentication

    Returns:
        tuple: (BatchV1Api, CoreV1Api, NetworkingV1Api) - Kubernetes API clients
        tuple: (None, None, None) - If setup fails
    """
    try:
        print("🔧 Starting Kubernetes client setup...")

        # Get service account credentials from environment variable
        creds_json = os.getenv("GCP_SA_KEY")
        if not creds_json:
            print("❌ GCP_SA_KEY environment variable not set")
            return None, None, None

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
        networking_api = k8s_client.NetworkingV1Api()

        # Test the connection
        print("🧪 Testing API connection...")
        namespaces = core_api.list_namespace(limit=1)
        print(f"✅ Successfully connected! Found {len(namespaces.items)} namespaces")

        print("✅ Kubernetes client setup complete!")

        return batch_api, core_api, networking_api

    except Exception as e:
        print(f"❌ Error setting up Kubernetes client: {e}")
        return None, None, None


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
            {"name": "JOB_NAME", "value": job_name},
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
            {
                "name": "UNITY_COMMS_URL",
                "value": "https://unity-comms-app-000000000000.us-central1.run.app",
            },
        ]
        if is_staging:
            env_vars = [
                {"name": "STAGING", "value": "true"},
                {
                    "name": "UNIFY_BASE_URL",
                    "value": "https://service.a.run.app/v0",
                },
                {
                    "name": "UNITY_COMMS_URL",
                    "value": "https://unity-comms-app-staging-000000000000.us-central1.run.app",
                },
            ] + env_vars[:-1]

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
                "backoffLimit": 2,  # Allow 1 retry for resource issues
                "template": {
                    "metadata": {"labels": {"app": "unity"}},
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
                                    {"containerPort": 6080},
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
            job_manifest["spec"]["ttlSecondsAfterFinished"] = ttl_seconds_after_finished

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


def create_external_service_for_job(
    core_api,
    job_name: str,
    namespace: str = "default",
    port: int = 6080,
    service_name: str = None,
    job_uid: str = None,
):
    """Create a ClusterIP Service for a Job (to be exposed via Ingress).

    The Service selects Pods with label `job-name=<job_name>` and exposes `port`.
    """
    try:
        name = service_name or f"unity-svc-{job_name}"
        service_manifest = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "app": "unity",
                    "job-name": job_name,
                },
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {
                    "job-name": job_name,
                },
                "ports": [
                    {
                        "name": f"http-{port}",
                        "port": port,
                        "targetPort": port,
                        "protocol": "TCP",
                    }
                ],
            },
        }

        # Attach ownerReferences so GC deletes Service when Job is deleted
        if job_uid:
            service_manifest["metadata"]["ownerReferences"] = [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": job_name,
                    "uid": job_uid,
                    # Not a controller of the Service; just ownership for GC
                    "controller": False,
                }
            ]

        # Try create; if exists, return existing
        try:
            svc = core_api.create_namespaced_service(
                namespace=namespace, body=service_manifest
            )
            print(f"✅ Service created: {name}")
            return svc
        except ApiException as e:
            if e.status == 409:
                print(f"⚠️  Service already exists: {name}")
                return core_api.read_namespaced_service(name=name, namespace=namespace)
            raise
    except Exception as e:
        print(f"❌ Error creating service: {e}")
        return None


def delete_service(core_api, service_name: str, namespace: str = "default"):
    """Delete a Kubernetes Service by name."""
    try:
        core_api.delete_namespaced_service(name=service_name, namespace=namespace)
        print(f"✅ Service deleted: {service_name}")
        return True
    except ApiException as e:
        if e.status == 404:
            print(f"⚠️  Service not found (already deleted): {service_name}")
            return True
        print(f"❌ Error deleting service: {e}")
        return False
    except Exception as e:
        print(f"❌ Error deleting service: {e}")
        return False


def add_ingress_rule_for_job(
    networking_api,
    job_name: str,
    service_name: str,
    namespace: str = "default",
    port: int = 6080,
    ingress_name: str = INGRESS_NAME,
    ingress_namespace: str = INGRESS_NAMESPACE,
):
    """Add a host-based rule to the shared Ingress for a job.

    Returns:
        dict: {"success": bool, "hostname": str, "url": str}
    """
    try:
        hostname = f"{job_name}.{DESKTOP_DOMAIN}"

        # Get current Ingress
        ingress = networking_api.read_namespaced_ingress(
            name=ingress_name,
            namespace=ingress_namespace,
        )

        # Build new rule
        new_rule = k8s_client.V1IngressRule(
            host=hostname,
            http=k8s_client.V1HTTPIngressRuleValue(
                paths=[
                    k8s_client.V1HTTPIngressPath(
                        path="/",
                        path_type="Prefix",
                        backend=k8s_client.V1IngressBackend(
                            service=k8s_client.V1IngressServiceBackend(
                                name=service_name,
                                port=k8s_client.V1ServiceBackendPort(number=port),
                            )
                        ),
                    )
                ]
            ),
        )

        # Check if rule already exists
        existing_rules = ingress.spec.rules or []
        for rule in existing_rules:
            if rule.host == hostname:
                print(f"⚠️  Ingress rule already exists for {hostname}")
                return {
                    "success": True,
                    "hostname": hostname,
                    "url": f"https://{hostname}",
                }

        # Add new rule
        existing_rules.append(new_rule)
        ingress.spec.rules = existing_rules

        # Patch the Ingress
        networking_api.patch_namespaced_ingress(
            name=ingress_name,
            namespace=ingress_namespace,
            body=ingress,
        )

        print(f"✅ Added Ingress rule for {hostname}")
        return {
            "success": True,
            "hostname": hostname,
            "url": f"https://{hostname}",
        }

    except ApiException as e:
        print(f"❌ Error adding Ingress rule: {e}")
        return {"success": False, "hostname": None, "url": None}
    except Exception as e:
        print(f"❌ Error adding Ingress rule: {e}")
        return {"success": False, "hostname": None, "url": None}


def remove_ingress_rule_for_job(
    networking_api,
    job_name: str,
    ingress_name: str = INGRESS_NAME,
    ingress_namespace: str = INGRESS_NAMESPACE,
):
    """Remove a host-based rule from the shared Ingress.

    Returns:
        bool: True if successful or rule didn't exist
    """
    try:
        hostname = f"{job_name}.{DESKTOP_DOMAIN}"

        # Get current Ingress
        ingress = networking_api.read_namespaced_ingress(
            name=ingress_name,
            namespace=ingress_namespace,
        )

        # Filter out the rule for this hostname
        existing_rules = ingress.spec.rules or []
        new_rules = [rule for rule in existing_rules if rule.host != hostname]

        if len(new_rules) == len(existing_rules):
            print(f"⚠️  No Ingress rule found for {hostname}")
            return True

        ingress.spec.rules = new_rules

        # Patch the Ingress
        networking_api.patch_namespaced_ingress(
            name=ingress_name,
            namespace=ingress_namespace,
            body=ingress,
        )

        print(f"✅ Removed Ingress rule for {hostname}")
        return True

    except ApiException as e:
        if e.status == 404:
            print(f"⚠️  Ingress not found: {ingress_name}")
            return True
        print(f"❌ Error removing Ingress rule: {e}")
        return False
    except Exception as e:
        print(f"❌ Error removing Ingress rule: {e}")
        return False


def check_service_has_endpoints(
    core_api,
    service_name: str,
    namespace: str = "default",
) -> dict:
    """Check if a Service has ready endpoints (pods backing it).

    Returns:
        dict: {"ready": bool, "ready_count": int, "message": str}
    """
    try:
        endpoints = core_api.read_namespaced_endpoints(
            name=service_name,
            namespace=namespace,
        )

        ready_count = 0
        if endpoints.subsets:
            for subset in endpoints.subsets:
                if subset.addresses:
                    ready_count += len(subset.addresses)

        if ready_count > 0:
            return {
                "ready": True,
                "ready_count": ready_count,
                "message": f"{ready_count} endpoint(s) ready",
            }
        else:
            return {
                "ready": False,
                "ready_count": 0,
                "message": "No ready endpoints (pod may still be starting)",
            }

    except ApiException as e:
        if e.status == 404:
            return {
                "ready": False,
                "ready_count": 0,
                "message": "Endpoints resource not found",
            }
        return {
            "ready": False,
            "ready_count": 0,
            "message": f"Error checking endpoints: {e.reason}",
        }
    except Exception as e:
        return {
            "ready": False,
            "ready_count": 0,
            "message": f"Error checking endpoints: {str(e)}",
        }


def check_ingress_rule_exists(
    networking_api,
    job_name: str,
    ingress_name: str = INGRESS_NAME,
    ingress_namespace: str = INGRESS_NAMESPACE,
) -> dict:
    """Check if an Ingress rule exists for this job's hostname.

    Returns:
        dict: {"exists": bool, "hostname": str, "message": str}
    """
    try:
        hostname = f"{job_name}.{DESKTOP_DOMAIN}"

        ingress = networking_api.read_namespaced_ingress(
            name=ingress_name,
            namespace=ingress_namespace,
        )

        rules = ingress.spec.rules or []
        for rule in rules:
            if rule.host == hostname:
                return {
                    "exists": True,
                    "hostname": hostname,
                    "message": "Ingress rule exists",
                }

        return {
            "exists": False,
            "hostname": hostname,
            "message": "Ingress rule not found",
        }

    except ApiException as e:
        if e.status == 404:
            return {
                "exists": False,
                "hostname": f"{job_name}.{DESKTOP_DOMAIN}",
                "message": "Ingress not found",
            }
        return {
            "exists": False,
            "hostname": f"{job_name}.{DESKTOP_DOMAIN}",
            "message": f"Error checking Ingress: {e.reason}",
        }
    except Exception as e:
        return {
            "exists": False,
            "hostname": f"{job_name}.{DESKTOP_DOMAIN}",
            "message": f"Error checking Ingress: {str(e)}",
        }


def get_job_readiness_status(
    core_api,
    networking_api,
    job_name: str,
    service_name: str,
    namespace: str = "default",
    gce_lb_wait_minutes: int = 5,
) -> dict:
    """Get comprehensive readiness status for a job.

    Ready = K8s checks pass AND 5 minutes have elapsed since job creation
    (to allow for GCE LB propagation).

    Returns:
        dict: {
            "ready": bool,
            "url": str,
            "hostname": str,
            "type": "ingress",
            "checks": {
                "service_exists": bool,
                "endpoints_ready": bool,
                "endpoints_count": int,
                "ingress_rule_exists": bool,
                "gce_lb_wait_passed": bool,
                "seconds_until_ready": int,
            }
        }
    """
    from datetime import datetime, timedelta

    hostname = f"{job_name}.{DESKTOP_DOMAIN}"
    url = f"https://{hostname}"

    checks = {
        "service_exists": False,
        "endpoints_ready": False,
        "endpoints_count": 0,
        "ingress_rule_exists": False,
        "gce_lb_wait_passed": False,
        "seconds_until_ready": 0,
    }

    # Check 1: Service exists
    try:
        core_api.read_namespaced_service(name=service_name, namespace=namespace)
        checks["service_exists"] = True
    except ApiException as e:
        if e.status == 404:
            checks["service_exists"] = False
        else:
            checks["service_exists"] = False
    except Exception:
        checks["service_exists"] = False

    # Check 2: Endpoints ready
    endpoints_result = check_service_has_endpoints(core_api, service_name, namespace)
    checks["endpoints_ready"] = endpoints_result["ready"]
    checks["endpoints_count"] = endpoints_result["ready_count"]

    # Check 3: Ingress rule exists
    ingress_result = check_ingress_rule_exists(networking_api, job_name)
    checks["ingress_rule_exists"] = ingress_result["exists"]

    # Check 4: GCE LB wait time (5 minutes since job creation)
    # Parse timestamp from job name: unity-2024-12-08-10-00-00 or unity-2024-12-08-10-00-00-staging
    try:
        # Remove prefix and suffix
        timestamp_str = job_name.replace("unity-", "").replace("-staging", "")
        job_created = datetime.strptime(timestamp_str, "%Y-%m-%d-%H-%M-%S")
        ready_at = job_created + timedelta(minutes=gce_lb_wait_minutes)
        now = datetime.now()

        if now >= ready_at:
            checks["gce_lb_wait_passed"] = True
            checks["seconds_until_ready"] = 0
        else:
            checks["gce_lb_wait_passed"] = False
            checks["seconds_until_ready"] = int((ready_at - now).total_seconds())
    except ValueError:
        # If we can't parse the timestamp, assume wait has passed
        checks["gce_lb_wait_passed"] = True
        checks["seconds_until_ready"] = 0

    # Overall ready = K8s checks pass AND GCE LB wait time has passed
    ready = all(
        [
            checks["service_exists"],
            checks["endpoints_ready"],
            checks["ingress_rule_exists"],
            checks["gce_lb_wait_passed"],
        ]
    )

    return {
        "ready": ready,
        "url": url,
        "hostname": hostname,
        "type": "ingress",
        "checks": checks,
    }
