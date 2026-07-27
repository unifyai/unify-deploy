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

    def _set_authorization_header(self) -> None:
        """Ensure the Kubernetes client sends a concrete Bearer token."""

        token = str(self._gke_creds.token or "").strip()
        if not token:
            raise RuntimeError("Missing GKE access token for Kubernetes API call")
        self.configuration.api_key_prefix["authorization"] = "Bearer"
        self.configuration.api_key["authorization"] = token

    def call_api(self, *args, **kwargs):
        with self._refresh_lock:
            if not self._gke_creds.valid or not self._gke_creds.token:
                self._gke_creds.refresh(self._auth_request)
            self._set_authorization_header()
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


def _merge_env_overrides(
    env_vars: list[dict],
    extra_env: dict[str, str] | None,
) -> list[dict]:
    """Apply explicit env values without leaving duplicate variable names."""

    if not extra_env:
        return env_vars
    remaining = {str(key): str(value) for key, value in extra_env.items() if str(key)}
    merged: list[dict] = []
    for env_var in env_vars:
        name = str(env_var.get("name", "") or "")
        if name in remaining:
            merged.append({"name": name, "value": remaining.pop(name)})
        else:
            merged.append(env_var)
    merged.extend({"name": name, "value": value} for name, value in remaining.items())
    return merged


def build_unity_job_manifest(
    job_name: str,
    namespace: str = "default",
    image: str = f"{SETTINGS.image_registry}/{SETTINGS.unity_image_name}:latest",
    deploy_env: str = SETTINGS.deploy_env,
    ttl_seconds_after_finished: int | None = None,
    active_deadline_seconds: int | None = None,
    unity_status: str = "idle",
    priority_class_name: str | None = None,
    app_label: str = "unity",
    extra_labels: dict | None = None,
    extra_annotations: dict | None = None,
    extra_env: dict[str, str] | None = None,
    backoff_limit: int = 0,
    termination_grace_period_seconds: int = 30,
) -> dict:
    """Build the Kubernetes ``batch/v1`` Job manifest for a Unity assistant.

    Pure data construction: no Kubernetes API calls, no logging, no
    exception swallowing. The caller decides what to do with the
    returned dict (submit it, dry-run it, render it as YAML, mutate
    it, ...). The canonical caller is :func:`create_unity_job` in
    this module, which builds + submits.

    The split exists so tests can assert manifest shape with direct
    dict assertions instead of mocking a Kubernetes Batch API client
    just to capture the body sent to ``create_namespaced_job``.

    Args:
        job_name: Name of the Job (also stamped into env as
            ``UNITY_CONVERSATION_JOB_NAME``).
        namespace: Target Kubernetes namespace.
        image: Container image. ``:latest`` tags get
            ``imagePullPolicy: Always``; any other tag (e.g. a SHA)
            gets ``IfNotPresent``.
        deploy_env: ``production`` | ``staging``. Drives env vars
            (``DEPLOY_ENV``, the gateway transports, ``UNITY_STARTUP_TIMING``,
            the pipeline artifact bucket name).
        ttl_seconds_after_finished: If set, applied to
            ``spec.ttlSecondsAfterFinished`` so finished Jobs garbage
            collect after this many seconds.
        active_deadline_seconds: If set, applied to
            ``spec.activeDeadlineSeconds`` so the Job is hard-killed
            after this duration.
        unity_status: Initial value of the ``unity-status`` label
            (``idle`` for pool jobs, ``running`` for controller-spawned
            jobs with image overrides, etc.).
        priority_class_name: Pod priority class. Defaults to
            ``unity-idle``.
        app_label: Value of the ``app`` label on both Job and pod
            template. Defaults to ``unity``; dashboard-action jobs use
            ``unity-dashboard-action``.
        extra_labels: Merged into Job ``metadata.labels`` (does NOT
            propagate to the pod template).
        extra_annotations: Merged into both Job
            ``metadata.annotations`` and the pod template's
            ``spec.template.metadata.annotations``.
        extra_env: Env vars added to the container. Vars whose name
            matches one already in the explicit env list override the
            earlier definition (see :func:`_merge_env_overrides`).
        backoff_limit: Kubernetes Job ``spec.backoffLimit``. Live
            assistant conversation Jobs stay at ``0`` (no pod restart;
            controller replaces work). Offline task Jobs pass a small
            positive value so a single transient pod failure can retry
            without waiting for the next scheduler tick.
        termination_grace_period_seconds: Pod
            ``spec.terminationGracePeriodSeconds``. Live conversation
            Jobs keep the short default (30). Offline task Jobs pass a
            longer grace so SIGTERM writeback can finish after a blocked
            outbound HTTP call (e.g. SmartLead's 60s client timeout)
            returns or is interrupted.
    """
    optional_unity_config_keys = {"UNITY_DEPLOY_RUNTIME_RECONCILE_MODE"}
    unity_config_env = []
    for key in (
        "GCP_PROJECT_ID",
        "PROJECT_ID",
        "VERTEXAI_LOCATION",
        "VERTEXAI_PROJECT",
        "UNITY_DEPLOY_RUNTIME_RECONCILE_MODE",
    ):
        config_ref = {
            "name": "unity-config",
            "key": key,
        }
        if key in optional_unity_config_keys:
            config_ref["optional"] = True
        unity_config_env.append(
            {
                "name": key,
                "valueFrom": {
                    "configMapKeyRef": config_ref,
                },
            },
        )
    # Meet twin credentials are per-environment and may be absent (an env whose
    # twin account is not yet provisioned). Marking them optional keeps the pod
    # bootable in that case; Meet then degrades to an anonymous guest join
    # instead of the whole assistant failing to start.
    optional_unity_secret_keys = {
        "MEET_TWIN_EMAIL",
        "MEET_TWIN_PASSWORD",
        "MEET_TWIN_TOTP_SECRET",
    }
    unity_secret_env = []
    for key in (
        "ANTHROPIC_API_KEY",
        "CARTESIA_API_KEY",
        "DEEPGRAM_API_KEY",
        "DEEPSEEK_API_KEY",
        "ELEVEN_API_KEY",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "LIVEKIT_SIP_URI",
        "LIVEKIT_URL",
        "MEET_TWIN_EMAIL",
        "MEET_TWIN_PASSWORD",
        "MEET_TWIN_TOTP_SECRET",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        # ORCHESTRA_ADMIN_KEY is intentionally NOT mounted: assistant pods
        # authenticate to Orchestra and the hosted gateway with their own
        # per-assistant UNIFY_KEY against ownership-scoped routes, so a
        # compromised pod can only act as itself. The platform admin key
        # stays on controllers / Cloud Run / reconcile jobs only.
        # SHARED_UNIFY_KEY is intentionally NOT mounted: AssistantJobs
        # writes go through /infra/assistant-jobs/* (admin key on comms).
        "TAVILY_API_KEY",
        "VERTEXAI_CREDENTIALS",
        "_UNITY_STARTUP_HOOK_GROUP",
        "_UNITY_STARTUP_HOOK_PACKAGE",
    ):
        secret_ref = {
            "name": "unity-secrets",
            "key": key,
        }
        if key in optional_unity_secret_keys:
            secret_ref["optional"] = True
        unity_secret_env.append(
            {
                "name": key,
                "valueFrom": {
                    "secretKeyRef": secret_ref,
                },
            },
        )

    env_vars = [
        {"name": "UNITY_CONVERSATION_JOB_NAME", "value": job_name},
        {"name": "DEPLOY_ENV", "value": deploy_env},
        # No GOOGLE_APPLICATION_CREDENTIALS: assistant Jobs get GCP creds from
        # the Workload Identity metadata server via assistant-runtime-sa.
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
        # Orchestra Events/*: keep only CodeAct execution rows. Pub/Sub Live
        # Actions streaming above stays full-fidelity (stream_filters only).
        {"name": "EVENTBUS_ORCHESTRA_PERSIST_MODE", "value": "allowlist"},
        {
            "name": "EVENTBUS_ORCHESTRA_PERSIST_TOOLS",
            "value": "execute_code,execute_function",
        },
        {"name": "UNITY_COMMS_URL", "value": SETTINGS.comms_url},
        {"name": "UNITY_ADAPTERS_URL", "value": SETTINGS.adapters_url},
        {"name": "ORCHESTRA_URL", "value": SETTINGS.orchestra_url},
        {
            "name": "UNITY_STARTUP_TIMING",
            "value": "0",
        },
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
        # Signed-in browser session for Google Meet joins: the CM hydrates
        # gs://{bucket}/{state}.json into the meet browser so it joins as the
        # per-env twin account instead of an anonymous guest (which Meet
        # rejects when no host is present). Absent blob -> graceful anonymous
        # fallback.
        {"name": "MEET_BROWSER_STATE_BUCKET", "value": "unity-browser-states"},
        {"name": "MEET_GOOGLE_STORAGE_STATE", "value": f"twin-session-{deploy_env}"},
        # Lets the renewal tick re-log in with the twin credentials above when
        # the cookies have already lapsed. Flip to "false" to make renewal
        # keep-warm only (it then alerts an operator instead of signing in).
        {"name": "BRAIN_MEET_AUTOLOGIN", "value": "true"},
    ]
    env_vars.extend(unity_config_env)
    env_vars.extend(unity_secret_env)
    if deploy_env == "staging":
        # Activate the new unify.gateway transports on staging Jobs
        # so the extracted Ingress + Outbound code paths get exercised
        # against real Pub/Sub traffic before any production cutover.
        # Production Jobs continue using the legacy inline
        # subscribe_to_topic and inline publisher.publish paths until
        # those paths are explicitly retired. See unity/gateway/PHASES.md
        # (Phase A.bis).
        env_vars += [
            {"name": "UNITY_CONVERSATION_INGRESS_TRANSPORT", "value": "pubsub"},
            {"name": "UNITY_CONVERSATION_OUTBOUND_TRANSPORT", "value": "pubsub"},
        ]
    env_vars = _merge_env_overrides(env_vars, extra_env)

    image_pull_policy = (
        "Always" if image.rsplit(":", 1)[-1] == "latest" else "IfNotPresent"
    )

    metadata_labels = {
        "app": app_label,
        "created-by": "create_job_script",
        "unity-status": unity_status,
        "unity-date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "unity-image-hash": (image.rsplit(":", 1)[-1] if ":" in image else "unknown"),
    }
    if extra_labels:
        metadata_labels.update(extra_labels)

    pod_annotations = {
        "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
    }
    if extra_annotations:
        pod_annotations.update(extra_annotations)

    metadata_annotations = dict(extra_annotations or {})

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
            "backoffLimit": backoff_limit,
            "template": {
                "metadata": {
                    "labels": {"app": app_label},
                    "annotations": pod_annotations,
                },
                "spec": {
                    "restartPolicy": "Never",
                    # Minimally-scoped Workload Identity SA (not fleet-wide
                    # comm-sa): GCP auth comes from the metadata server via WI,
                    # so no JSON key is mounted and a compromised pod cannot use
                    # comm-sa's broad GCS/compute access.
                    "serviceAccountName": "assistant-runtime-sa",
                    "terminationGracePeriodSeconds": termination_grace_period_seconds,
                    "priorityClassName": (priority_class_name or "unity-idle"),
                    "containers": [
                        {
                            "name": "unity-assistant",
                            "image": image,
                            "imagePullPolicy": image_pull_policy,
                            "ports": [
                                {"containerPort": 8000},
                                {"containerPort": 6379},
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
                                    "name": "tmp-vol",
                                    "mountPath": "/tmp",
                                },
                            ],
                        },
                    ],
                    "volumes": [
                        {"name": "tmp-vol", "emptyDir": {}},
                    ],
                },
            },
        },
    }

    if ttl_seconds_after_finished is not None:
        job_manifest["spec"]["ttlSecondsAfterFinished"] = ttl_seconds_after_finished
    if active_deadline_seconds is not None:
        job_manifest["spec"]["activeDeadlineSeconds"] = active_deadline_seconds

    return job_manifest


def create_unity_job(
    batch_api,
    job_name: str,
    namespace: str = "default",
    image: str = f"{SETTINGS.image_registry}/{SETTINGS.unity_image_name}:latest",
    deploy_env: str = SETTINGS.deploy_env,
    ttl_seconds_after_finished: int | None = None,
    active_deadline_seconds: int | None = None,
    unity_status: str = "idle",
    priority_class_name: str | None = None,
    app_label: str = "unity",
    extra_labels: dict | None = None,
    extra_annotations: dict | None = None,
    extra_env: dict[str, str] | None = None,
):
    """Build and submit a Kubernetes Job for a Unity assistant.

    Thin wrapper around :func:`build_unity_job_manifest`: builds the
    manifest from the same arguments, then calls
    ``batch_api.create_namespaced_job(...)``.

    Behaviour preserved bit-for-bit from before the build/submit
    split:

    * Returns the K8s API response object (with ``.metadata.name`` /
      ``.metadata.uid``) on success.
    * Returns ``None`` on a 409 Conflict (Job already exists) -- the
      idle-pool replenish path relies on this to gracefully race with
      itself.
    * Returns ``None`` on any other ``ApiException`` (or unrelated
      exception), with an error printed to stdout. **Production
      callers depend on this swallow-and-return-None contract** --
      see the test for /infra/job/create's response handling.

    All keyword arguments are forwarded to
    :func:`build_unity_job_manifest` -- see that function for full
    parameter docs.
    """
    try:
        job_manifest = build_unity_job_manifest(
            job_name=job_name,
            namespace=namespace,
            image=image,
            deploy_env=deploy_env,
            ttl_seconds_after_finished=ttl_seconds_after_finished,
            active_deadline_seconds=active_deadline_seconds,
            unity_status=unity_status,
            priority_class_name=priority_class_name,
            app_label=app_label,
            extra_labels=extra_labels,
            extra_annotations=extra_annotations,
            extra_env=extra_env,
        )
        try:
            api_response = batch_api.create_namespaced_job(
                namespace=namespace,
                body=job_manifest,
            )
            print("✅ Job created successfully!")
            print(f"   Job name: {api_response.metadata.name}")
            print(f"   Job UID: {api_response.metadata.uid}")
            print(f"   Namespace: {namespace}")
            print(f"   Image: {image}")
            return api_response
        except ApiException as exc:
            if exc.status == 409:
                print(f"⚠️  Job already exists: {job_name}")
                return None
            raise
    except Exception as exc:
        print(f"❌ Error creating job: {exc}")
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
