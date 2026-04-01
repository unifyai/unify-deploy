"""
Shared fixtures for infrastructure integration tests.

These tests run against real deployed K8s and GCE infrastructure.
Every test creates its own resources and cleans up in finally blocks.

Configuration:
    Copy tests/infra/integration/.env.example to .env and fill in your
    credentials. The .env file is gitignored. See README.md for details.
"""

import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import pytest
import requests
from dotenv import load_dotenv

# Load env vars from (in priority order):
# 1. tests/infra/integration/.env (local test config, gitignored)
# 2. The unity repo's .env (fallback for shared keys)
# 3. Shell environment (highest priority, overrides everything)
_test_env = Path(__file__).parent / ".env"
_unity_env = Path.home() / "Unify" / "unity" / ".env"

if _test_env.is_file():
    load_dotenv(_test_env)
elif _unity_env.is_file():
    load_dotenv(_unity_env)
load_dotenv()  # shell env overrides

# ---------------------------------------------------------------------------
# Deployment configuration (all overridable via env vars)
# ---------------------------------------------------------------------------

GCP_PROJECT_ID = os.getenv("TEST_GCP_PROJECT_ID", "gcp-project-runtime")
VM_PROJECT_ID = os.getenv("TEST_VM_PROJECT_ID", "gcp-project-vms")
GKE_CLUSTER = os.getenv("TEST_GKE_CLUSTER", "unity")
GKE_REGION = os.getenv("TEST_GKE_REGION", "us-central1")
NAMESPACE = os.getenv("TEST_NAMESPACE", "staging")
VM_ZONE = os.getenv("TEST_VM_ZONE", "us-central1-a")

COMMS_APP_URL = os.getenv(
    "TEST_COMMS_APP_URL",
    "https://service.a.run.app",
)
ADAPTERS_URL = os.getenv(
    "TEST_ADAPTERS_URL",
    "https://service.a.run.app",
)

ADMIN_KEY = os.getenv("ORCHESTRA_ADMIN_KEY", "")
SHARED_KEY = os.getenv("SHARED_UNIFY_KEY", "")
UNIFY_KEY = os.getenv("UNIFY_KEY", "")

ORCHESTRA_URL = os.getenv(
    "TEST_ORCHESTRA_URL",
    "https://internal.example.com/v0",
)

TEST_ASSISTANT_ID = os.getenv("TEST_ASSISTANT_ID", "")


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: tests that run against real deployed infrastructure",
    )
    config.addinivalue_line(
        "markers",
        "invariant: which INV-N invariant(s) the test covers",
    )
    config.addinivalue_line(
        "markers",
        "slow: long-running tests (VM provision, etc.) — deselect with -m 'not slow'",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


# ---------------------------------------------------------------------------
# Test ID generation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def real_assistant_data():
    """Fetch a real assistant's full data from Orchestra.

    Uses the same admin endpoint the adapter uses (GET /admin/assistant),
    so the data is identical to what flows through production.

    Configure which assistant to use via TEST_ASSISTANT_ID in .env.
    If not set, uses the first assistant found for the current user.
    """
    assert ADMIN_KEY, "ORCHESTRA_ADMIN_KEY required to fetch assistant data"

    assistant_id = TEST_ASSISTANT_ID
    if not assistant_id:
        assert (
            UNIFY_KEY
        ), "UNIFY_KEY required to auto-detect assistant (or set TEST_ASSISTANT_ID)"
        user_resp = requests.get(
            f"{ORCHESTRA_URL}/assistant",
            headers={"Authorization": f"Bearer {UNIFY_KEY}"},
            timeout=10,
        )
        assert user_resp.status_code == 200, (
            f"Failed to list assistants: {user_resp.status_code}. "
            "Set TEST_ASSISTANT_ID in .env to specify an assistant explicitly."
        )
        data = user_resp.json()
        assistants = data.get("info", data) if isinstance(data, dict) else data
        assert (
            assistants
        ), "No assistants found for this user. Hire one on the target environment first."
        if NAMESPACE == "preview":
            preview_candidates = [
                a
                for a in assistants
                if a.get("deploy_env") == "preview" and not a.get("is_local", False)
            ]
            if preview_candidates:
                chosen = preview_candidates[0]
            else:
                print(
                    "\n[Setup] No non-local preview assistant found; "
                    "creating one automatically for preview integration tests.",
                )
                return _create_preview_managed_assistant()
        else:
            chosen = assistants[0]
        assistant_id = str(chosen["agent_id"])
        print(
            f"\n[Setup] Auto-detected assistant: {chosen.get('first_name', '')} "
            f"{chosen.get('surname', '')} (ID {assistant_id})",
        )

    resp = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params={"agent_id": assistant_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=10,
    )
    assert (
        resp.status_code == 200
    ), f"Failed to fetch assistant {assistant_id}: {resp.status_code} {resp.text}"
    a = resp.json()["info"][0]

    return _admin_record_to_data(a)


@pytest.fixture
def test_id(real_assistant_data):
    """The real assistant ID from Orchestra."""
    return real_assistant_data["assistant_id"]


# ---------------------------------------------------------------------------
# Auth headers
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_headers():
    assert ADMIN_KEY, "ORCHESTRA_ADMIN_KEY must be set"
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture
def shared_headers():
    assert SHARED_KEY, "SHARED_UNIFY_KEY must be set"
    return {"Authorization": f"Bearer {SHARED_KEY}"}


# ---------------------------------------------------------------------------
# K8s client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def k8s_clients():
    """Authenticated K8s BatchV1Api and CoreV1Api for the target cluster."""
    from kubernetes import client, config

    try:
        config.load_kube_config()
    except Exception:
        config.load_incluster_config()

    return client.BatchV1Api(), client.CoreV1Api()


@pytest.fixture
def batch_api(k8s_clients):
    return k8s_clients[0]


@pytest.fixture
def core_api(k8s_clients):
    return k8s_clients[1]


# ---------------------------------------------------------------------------
# GCE client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def gce_client():
    """GCE client using gcloud CLI credentials.

    The default ADC credentials may not have compute.instances.list permission
    on gcp-project-vms. We use the gcloud CLI's own access token instead,
    which has the full scope set from `gcloud auth login`.

    Returns None if GCE access fails (VM tests will skip).
    """
    from google.cloud import compute_v1

    try:
        from google.oauth2 import credentials as oauth2_credentials

        def _refresh_access_token(request=None, scopes=None):
            import subprocess

            token = subprocess.check_output(
                ["gcloud", "auth", "print-access-token"],
                text=True,
                timeout=10,
            ).strip()
            expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=50)
            return token, expiry

        token, expiry = _refresh_access_token()
        creds = oauth2_credentials.Credentials(
            token=token,
            expiry=expiry,
            refresh_handler=_refresh_access_token,
        )
        client = compute_v1.InstancesClient(credentials=creds)

        request = compute_v1.ListInstancesRequest(
            project=VM_PROJECT_ID,
            zone=VM_ZONE,
            filter="labels.vm-type=ubuntu AND labels.pool-role=idle AND status=RUNNING",
            max_results=1,
        )
        result = list(client.list(request=request))
        print(f"\n[GCE] Connected, found {len(result)} idle VMs in probe")
        return client
    except Exception as e:
        print(f"\n[GCE client unavailable] {type(e).__name__}: {e}")
        return None


def require_gce(gce_client):
    """Skip test if GCE is not accessible."""
    if gce_client is None:
        pytest.skip("No GCE compute.instances.list permission on gcp-project-vms")


# ---------------------------------------------------------------------------
# Pub/Sub client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pubsub_publisher():
    from google.cloud import pubsub_v1

    return pubsub_v1.PublisherClient()


# ---------------------------------------------------------------------------
# Polling helper
# ---------------------------------------------------------------------------


def poll_until(
    condition: Callable[[], Any],
    timeout: float = 120,
    interval: float = 5,
    description: str = "condition",
    failure_snapshot: Callable[[], Any] | None = None,
) -> Any:
    """Poll until condition() returns a truthy value, or raise on timeout."""
    deadline = time.monotonic() + timeout
    last_result = None
    while time.monotonic() < deadline:
        last_result = condition()
        if last_result:
            return last_result
        time.sleep(interval)
    snapshot_text = ""
    if failure_snapshot is not None:
        try:
            snapshot = failure_snapshot()
            snapshot_text = "\nFailure snapshot:\n" + json.dumps(
                snapshot,
                indent=2,
                sort_keys=True,
                default=str,
            )
        except Exception as exc:
            snapshot_text = (
                f"\nFailure snapshot unavailable: {type(exc).__name__}: {exc}"
            )
    raise TimeoutError(
        f"Timed out after {timeout}s waiting for {description}. "
        f"Last result: {last_result}.{snapshot_text}",
    )


@pytest.fixture
def poll():
    """Polling helper fixture."""
    return poll_until


def _sanitize_artifact_name(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", nodeid).strip("_")


def _cloud_run_service_name(service_url: str) -> str | None:
    hostname = urlparse(service_url).hostname or ""
    if not hostname:
        return None
    first_label = hostname.split(".")[0]
    parts = first_label.split("-")
    if len(parts) >= 3:
        return "-".join(parts[:-2])
    return first_label


def _assistant_ids_from_violation_messages(new_violations) -> list[str]:
    assistant_ids = set()
    patterns = [
        r"assistant-id=([A-Za-z0-9._-]+)",
        r"assistant ([A-Za-z0-9._-]+)",
        r"assigned to ([A-Za-z0-9._-]+)",
    ]
    for violation in new_violations:
        message = getattr(violation, "message", "")
        for pattern in patterns:
            assistant_ids.update(re.findall(pattern, message))
    return sorted(assistant_ids)


def _assistant_ids_from_request(request, batch_api) -> list[str]:
    assistant_ids = set()
    funcargs = getattr(request.node, "funcargs", {})

    real_assistant = funcargs.get("real_assistant_data")
    if isinstance(real_assistant, dict) and real_assistant.get("assistant_id"):
        assistant_ids.add(str(real_assistant["assistant_id"]))

    test_id = funcargs.get("test_id")
    if test_id:
        assistant_ids.add(str(test_id))

    test_assistants = funcargs.get("test_assistants") or []
    for assistant in test_assistants:
        if isinstance(assistant, dict) and assistant.get("assistant_id"):
            assistant_ids.add(str(assistant["assistant_id"]))

    job_tracker = funcargs.get("job_tracker")
    if job_tracker is not None:
        for job_name in getattr(job_tracker, "jobs", []):
            try:
                job = batch_api.read_namespaced_job(name=job_name, namespace=NAMESPACE)
            except Exception:
                continue
            labels = dict(job.metadata.labels or {})
            if labels.get("assistant-id"):
                assistant_ids.add(str(labels["assistant-id"]))

    return sorted(assistant_ids)


def _session_snapshot(assistant_id: str) -> dict[str, Any]:
    try:
        resp = requests.get(
            f"{COMMS_APP_URL}/infra/session/{assistant_id}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=20,
        )
    except requests.RequestException as exc:
        return {"assistant_id": str(assistant_id), "error": str(exc)}
    if resp.status_code == 404:
        return {"assistant_id": str(assistant_id), "session": None}
    if resp.status_code != 200:
        return {
            "assistant_id": str(assistant_id),
            "error": f"session HTTP {resp.status_code}: {resp.text[:500]}",
        }
    return {"assistant_id": str(assistant_id), "session": resp.json()}


def _job_summaries(batch_api, assistant_id: str) -> list[dict[str, Any]]:
    sanitized = str(assistant_id).lower().replace("_", "-")
    jobs = batch_api.list_namespaced_job(
        namespace=NAMESPACE,
        label_selector=f"app=unity,assistant-id={sanitized}",
    )
    results = []
    for job in jobs.items:
        labels = dict(job.metadata.labels or {})
        annotations = dict(job.metadata.annotations or {})
        results.append(
            {
                "job_name": job.metadata.name,
                "active": job.status.active or 0,
                "succeeded": job.status.succeeded or 0,
                "failed": job.status.failed or 0,
                "labels": labels,
                "annotations": {
                    key: value
                    for key, value in annotations.items()
                    if key.startswith("assistantsession.")
                },
                "creation_timestamp": (
                    job.metadata.creation_timestamp.isoformat()
                    if job.metadata.creation_timestamp
                    else None
                ),
            },
        )
    return results


def _pod_summaries(core_api, job_names: list[str]) -> list[dict[str, Any]]:
    pods = []
    if core_api is None:
        return pods
    for job_name in job_names:
        try:
            pod_list = core_api.list_namespaced_pod(
                namespace=NAMESPACE,
                label_selector=f"job-name={job_name}",
            )
        except Exception:
            continue
        for pod in pod_list.items:
            pods.append(
                {
                    "pod_name": pod.metadata.name,
                    "job_name": job_name,
                    "phase": pod.status.phase,
                    "node_name": pod.spec.node_name,
                },
            )
    return pods


def describe_pool_state(gce_client, vm_type: str = "ubuntu") -> dict[str, Any]:
    from google.cloud import compute_v1

    try:
        client = compute_v1.InstancesClient()
        request = compute_v1.ListInstancesRequest(
            project=VM_PROJECT_ID,
            zone=VM_ZONE,
            filter=f"labels.pool-role:* AND labels.vm-type={vm_type}",
        )
        pool_vms = []
        for instance in client.list(request=request):
            labels = dict(instance.labels) if instance.labels else {}
            entry: dict[str, Any] = {
                "vm_name": instance.name,
                "pool_role": labels.get("pool-role", "unknown"),
                "assistant_id": labels.get("assistant-id", "") or None,
                "status": instance.status,
            }
            if instance.last_start_timestamp:
                entry["last_start_timestamp"] = instance.last_start_timestamp
            if instance.last_stop_timestamp:
                entry["last_stop_timestamp"] = instance.last_stop_timestamp
            pool_vms.append(entry)
    except Exception as exc:
        return {"vm_type": vm_type, "zone": VM_ZONE, "error": str(exc)}

    grouped = {
        "idle": [],
        "assigned": [],
        "stopped": [],
        "provisioning": [],
        "starting": [],
        "quarantined": [],
        "other": [],
    }
    for vm in pool_vms:
        role = vm.get("pool_role") or "other"
        grouped.setdefault(role, []).append(vm)

    DETAIL_ROLES = {"starting", "provisioning", "assigned"}

    def _vm_summary(vm: dict) -> dict:
        entry: dict[str, Any] = {"name": vm.get("vm_name")}
        for ts_field in ("last_start_timestamp", "last_stop_timestamp"):
            if vm.get(ts_field):
                entry[ts_field] = vm[ts_field]
        if vm.get("assistant_id"):
            entry["assistant_id"] = vm["assistant_id"]
        return entry

    return {
        "vm_type": vm_type,
        "zone": VM_ZONE,
        "counts": {role: len(vms) for role, vms in grouped.items()},
        "vm_names": {
            role: [vm.get("vm_name") for vm in vms]
            for role, vms in grouped.items()
            if vms and role not in DETAIL_ROLES
        },
        "vm_detail": {
            role: [_vm_summary(vm) for vm in vms]
            for role, vms in grouped.items()
            if vms and role in DETAIL_ROLES
        },
    }


def describe_runtime_state(
    batch_api,
    core_api,
    gce_client,
    assistant_id: str,
) -> dict[str, Any]:
    session_snapshot = _session_snapshot(str(assistant_id))
    jobs = _job_summaries(batch_api, str(assistant_id))
    job_names = [job["job_name"] for job in jobs]
    pods = _pod_summaries(core_api, job_names)
    assigned_vms = (
        list_assigned_vms(gce_client, str(assistant_id)) if gce_client else []
    )
    records = get_assistant_jobs_records(str(assistant_id), running_only=False)
    return {
        "assistant_id": str(assistant_id),
        "session": session_snapshot.get("session"),
        "session_error": session_snapshot.get("error"),
        "jobs": jobs,
        "pods": pods,
        "assigned_vms": [
            {
                "vm_name": vm.name,
                "labels": dict(vm.labels or {}),
                "status": vm.status,
            }
            for vm in assigned_vms
        ],
        "assistant_job_records": records,
    }


def _recent_cloud_run_logs(
    service_url: str,
    assistant_ids: list[str],
    session_names: list[str],
) -> list[str]:
    service_name = _cloud_run_service_name(service_url)
    if not service_name:
        return []
    since = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        output = subprocess.check_output(
            [
                "gcloud",
                "logging",
                "read",
                (
                    f'resource.type="cloud_run_revision" AND '
                    f'resource.labels.service_name="{service_name}" AND '
                    f'timestamp >= "{since}"'
                ),
                "--project=gcp-project-runtime",
                "--limit=200",
                "--format=value(timestamp,textPayload)",
            ],
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return [f"log collection failed for {service_name}: {exc}"]

    noise_markers = ("_bio_emb", "_emb", "embedding", "derived_entries")
    terms = [str(term) for term in assistant_ids + session_names if term]
    lines = []
    for line in output.splitlines():
        if not any(term in line for term in terms):
            continue
        if any(marker in line for marker in noise_markers):
            continue
        if len(line) > 2000:
            line = line[:2000] + "... [truncated]"
        lines.append(line)
    return lines[-80:]


def _recent_controller_logs(
    assistant_ids: list[str],
    session_names: list[str],
) -> list[str]:
    try:
        pods_output = subprocess.check_output(
            ["kubectl", "get", "pods", "-n", NAMESPACE, "-o", "name"],
            text=True,
            timeout=20,
        )
    except Exception as exc:
        return [f"controller pod lookup failed: {exc}"]

    controller_pod = next(
        (
            line.split("/", 1)[1]
            for line in pods_output.splitlines()
            if "assistant-session-controller" in line
        ),
        None,
    )
    if controller_pod is None:
        return []

    try:
        output = subprocess.check_output(
            [
                "kubectl",
                "logs",
                "-n",
                NAMESPACE,
                f"pod/{controller_pod}",
                "--since=10m",
            ],
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return [f"controller log collection failed: {exc}"]

    terms = [str(term) for term in assistant_ids + session_names if term]
    lines = [
        line for line in output.splitlines() if any(term in line for term in terms)
    ]
    return lines[-80:]


def _write_failure_artifact(bundle: dict[str, Any]) -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    artifact_dir = Path("logs/pytest/integration-failures")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = (
        artifact_dir
        / f"{timestamp}_{_sanitize_artifact_name(bundle['test_nodeid'])}.json"
    )
    artifact_path.write_text(json.dumps(bundle, indent=2, sort_keys=True, default=str))
    return str(artifact_path)


def _build_failure_artifact(
    request,
    batch_api,
    core_api,
    gce_client,
    new_violations,
) -> str:
    assistant_ids = sorted(
        set(_assistant_ids_from_request(request, batch_api))
        | set(_assistant_ids_from_violation_messages(new_violations)),
    )
    runtime = {
        assistant_id: describe_runtime_state(
            batch_api,
            core_api,
            gce_client,
            assistant_id,
        )
        for assistant_id in assistant_ids
    }
    session_names = [
        ((state.get("session") or {}).get("metadata") or {}).get("name")
        for state in runtime.values()
        if state.get("session")
    ]
    bundle = {
        "test_nodeid": request.node.nodeid,
        "timestamp": datetime.now(UTC).isoformat(),
        "assistant_ids": assistant_ids,
        "runtime": runtime,
        "pool_state": describe_pool_state(gce_client),
        "new_invariant_violations": [
            {
                "invariant_id": v.invariant_id,
                "message": v.message,
            }
            for v in new_violations
        ],
        "recent_logs": {
            "controller": _recent_controller_logs(assistant_ids, session_names),
            "comms": _recent_cloud_run_logs(
                COMMS_APP_URL,
                assistant_ids,
                session_names,
            ),
            "adapters": _recent_cloud_run_logs(
                ADAPTERS_URL,
                assistant_ids,
                session_names,
            ),
        },
        "active_jobs_overview": [
            {
                "job_name": job.metadata.name,
                "assistant_id": (job.metadata.labels or {}).get("assistant-id"),
                "unity_status": (job.metadata.labels or {}).get("unity-status"),
            }
            for job in batch_api.list_namespaced_job(
                namespace=NAMESPACE,
                label_selector="app=unity",
            ).items
            if job.status.active and job.status.active > 0
        ],
    }
    return _write_failure_artifact(bundle)


# ---------------------------------------------------------------------------
# Comms App HTTP helpers
# ---------------------------------------------------------------------------


@dataclass
class CommsClient:
    """HTTP client for the deployed Comms App."""

    base_url: str = COMMS_APP_URL
    headers: dict = field(
        default_factory=lambda: {"Authorization": f"Bearer {ADMIN_KEY}"},
    )

    def post(self, path: str, **kwargs) -> requests.Response:
        return requests.post(f"{self.base_url}{path}", headers=self.headers, **kwargs)

    def get(self, path: str, **kwargs) -> requests.Response:
        return requests.get(f"{self.base_url}{path}", headers=self.headers, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return requests.delete(f"{self.base_url}{path}", headers=self.headers, **kwargs)

    def patch(self, path: str, **kwargs) -> requests.Response:
        return requests.patch(f"{self.base_url}{path}", headers=self.headers, **kwargs)


@dataclass
class AdaptersClient:
    """HTTP client for the deployed Adapters service."""

    base_url: str = ADAPTERS_URL
    headers: dict = field(
        default_factory=lambda: {"Authorization": f"Bearer {ADMIN_KEY}"},
    )

    def post(self, path: str, **kwargs) -> requests.Response:
        return requests.post(f"{self.base_url}{path}", headers=self.headers, **kwargs)

    def get(self, path: str, **kwargs) -> requests.Response:
        return requests.get(f"{self.base_url}{path}", headers=self.headers, **kwargs)


@pytest.fixture
def comms(admin_headers):
    return CommsClient()


@pytest.fixture
def adapters(admin_headers):
    return AdaptersClient()


# ---------------------------------------------------------------------------
# K8s Job helpers
# ---------------------------------------------------------------------------


def start_real_job(comms_client, assistant_data: dict, medium: str = "unify_message"):
    """Start a job using real assistant data via the Comms App.

    Identical to the production code path: adapter calls /infra/job/start
    with the full assistant payload from Orchestra.
    """
    resp = comms_client.post(
        "/infra/job/start",
        data={
            "api_key": assistant_data["api_key"],
            "medium": medium,
            **{k: v for k, v in assistant_data.items() if k != "api_key"},
        },
    )
    assert resp.status_code == 200, f"job/start failed: {resp.status_code} {resp.text}"
    return resp


def get_assistant_session(comms_client, assistant_id: str) -> dict | None:
    """Read the AssistantSession for an assistant from the Comms app."""
    resp = comms_client.get(f"/infra/session/{assistant_id}")
    if resp.status_code == 404:
        return None
    assert (
        resp.status_code == 200
    ), f"session read failed: {resp.status_code} {resp.text}"
    return resp.json()


def replenish_pool():
    """Trigger idle pool replenishment on the deployed adapters.

    Call this after tests that consume idle containers to ensure the pool
    is refilled for subsequent tests.
    """
    try:
        resp = requests.post(
            f"{ADAPTERS_URL}/scheduled/jobs/create",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            created = data.get("created", 0)
            if created > 0:
                print(f"\n[Pool] Replenished {created} idle container(s)")
    except Exception:
        pass


def wait_for_idle_pool(batch_api, min_idle: int = 1, timeout: float = 90):
    """Wait until the idle pool has at least min_idle containers."""
    poll_until(
        lambda: count_idle_jobs(batch_api) >= min_idle,
        timeout=timeout,
        interval=10,
        description=f"Idle pool to have >= {min_idle} containers",
        failure_snapshot=lambda: {
            "idle_count": count_idle_jobs(batch_api),
            "active_jobs": [
                {
                    "job_name": job.metadata.name,
                    "assistant_id": (job.metadata.labels or {}).get("assistant-id"),
                    "unity_status": (job.metadata.labels or {}).get("unity-status"),
                }
                for job in batch_api.list_namespaced_job(
                    namespace=NAMESPACE,
                    label_selector="app=unity",
                ).items
                if job.status.active and job.status.active > 0
            ],
        },
    )


@dataclass
class JobTracker:
    """Tracks created Jobs for cleanup + pool replenishment."""

    jobs: list = field(default_factory=list)
    batch_api: Any = None
    namespace: str = NAMESPACE

    def track(self, job_name: str):
        self.jobs.append(job_name)

    def cleanup(self):
        if not self.batch_api:
            return
        for name in self.jobs:
            try:
                self.batch_api.delete_namespaced_job(
                    name=name,
                    namespace=self.namespace,
                    propagation_policy="Foreground",
                )
            except Exception:
                pass
        if self.jobs:
            replenish_pool()


@pytest.fixture
def job_tracker(batch_api):
    tracker = JobTracker(batch_api=batch_api)
    yield tracker
    tracker.cleanup()


@pytest.fixture(autouse=True)
def ensure_pool_capacity(request, k8s_clients):
    """Before each test, ensure the idle pool has at least 1 container.

    If a previous test consumed containers and replenishment hasn't finished,
    wait up to 90s for the pool to refill. This prevents cascading failures
    from pool exhaustion.
    """
    batch_api = k8s_clients[0]
    idle = count_idle_jobs(batch_api)
    if idle < 1:
        print(f"\n[Pool] Only {idle} idle containers, triggering replenishment...")
        replenish_pool()
        try:
            wait_for_idle_pool(batch_api, min_idle=1, timeout=90)
        except TimeoutError:
            print("[Pool] WARNING: Could not replenish pool within 90s")
    yield


def get_job_labels(batch_api, job_name: str, namespace: str = NAMESPACE) -> dict:
    """Read current labels from a K8s Job."""
    job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
    return dict(job.metadata.labels or {})


def get_job_resource_version(
    batch_api,
    job_name: str,
    namespace: str = NAMESPACE,
) -> str:
    """Read current resourceVersion from a K8s Job."""
    job = batch_api.read_namespaced_job(name=job_name, namespace=namespace)
    return job.metadata.resource_version


def list_jobs_with_assistant_id(
    batch_api,
    assistant_id: str,
    namespace: str = NAMESPACE,
) -> list:
    """List all Jobs with a specific assistant-id label."""
    sanitized = assistant_id.lower().replace("_", "-")
    jobs = batch_api.list_namespaced_job(
        namespace=namespace,
        label_selector=f"app=unity,assistant-id={sanitized}",
    )
    return [j for j in jobs.items if j.status.active and j.status.active > 0]


def list_jobs_with_session_ref(
    batch_api,
    session_name: str,
    namespace: str = NAMESPACE,
) -> list:
    jobs = batch_api.list_namespaced_job(
        namespace=namespace,
        label_selector=f"assistantsession.unify.ai/name={session_name}",
    )
    return [j for j in jobs.items if j.status.active and j.status.active > 0]


def count_idle_jobs(batch_api, namespace: str = NAMESPACE) -> int:
    """Count Jobs with unity-status=idle and active pods."""
    jobs = batch_api.list_namespaced_job(
        namespace=namespace,
        label_selector="app=unity,unity-status=idle",
    )
    return sum(1 for j in jobs.items if j.status.active and j.status.active > 0)


# ---------------------------------------------------------------------------
# AssistantJobs helpers
# ---------------------------------------------------------------------------


def get_assistant_jobs_records(assistant_id: str, running_only: bool = True) -> list:
    """Query AssistantJobs for records matching this assistant."""
    filter_expr = f"assistant_id == '{assistant_id}'"
    if running_only:
        filter_expr += " and running == 'true'"

    resp = requests.get(
        f"{ORCHESTRA_URL}/logs",
        params={
            "project_name": "AssistantJobs",
            "context": "startup_events",
            "filter_expr": filter_expr,
            "limit": 50,
        },
        headers={"Authorization": f"Bearer {SHARED_KEY}"},
        timeout=10,
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("logs", [])


def expire_test_assistant_records(assistant_id: str):
    """Clean up any AssistantJobs records for a test assistant."""
    records = get_assistant_jobs_records(assistant_id, running_only=True)
    if not records:
        return
    record_ids = [r["id"] for r in records if "id" in r]
    if record_ids:
        try:
            requests.put(
                f"{ORCHESTRA_URL}/logs",
                json={
                    "logs": record_ids,
                    "context": "startup_events",
                    "entries": {"running": False},
                    "overwrite": True,
                },
                headers={"Authorization": f"Bearer {SHARED_KEY}"},
                timeout=10,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Assistant cleanup (shared across test modules)
# ---------------------------------------------------------------------------


def cleanup_assistant_jobs(batch_api, assistant_ids: list[str]):
    """Delete Jobs, release VMs, and expire records for assistant IDs."""
    for aid in assistant_ids:
        expire_test_assistant_records(str(aid))
        sanitized = str(aid).lower().replace("_", "-")
        try:
            jobs = batch_api.list_namespaced_job(
                namespace=NAMESPACE,
                label_selector=f"app=unity,assistant-id={sanitized}",
            )
            for job in jobs.items:
                try:
                    batch_api.delete_namespaced_job(
                        name=job.metadata.name,
                        namespace=NAMESPACE,
                        propagation_policy="Foreground",
                    )
                except Exception:
                    pass
        except Exception:
            pass
        try:
            requests.post(
                f"{COMMS_APP_URL}/infra/vm/pool/release",
                json={"assistant_id": str(aid)},
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                timeout=30,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Credits helpers
# ---------------------------------------------------------------------------


def _get_user_id_from_key() -> str:
    """Resolve the user_id for the current UNIFY_KEY."""
    resp = requests.get(
        f"{ORCHESTRA_URL}/user/basic-info",
        headers={"Authorization": f"Bearer {UNIFY_KEY}"},
        timeout=10,
    )
    if resp.status_code == 200:
        data = resp.json()
        info = data.get("info", data) if isinstance(data, dict) else data
        if isinstance(info, dict):
            return info.get("user_id", info.get("id", ""))
        if isinstance(info, list) and info:
            return info[0].get("user_id", info[0].get("id", ""))
    return ""


def _ensure_credits(min_credits: float):
    """Top up credits if the current balance is below *min_credits*.

    Non-production Orchestra may skip credit checks, so this is a safety net
    for production environments only.  Uses the admin create_recharge
    endpoint with type="promo".
    """
    if not UNIFY_KEY or not ADMIN_KEY:
        return

    try:
        resp = requests.get(
            f"{ORCHESTRA_URL}/credits",
            headers={"Authorization": f"Bearer {UNIFY_KEY}"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[Credits] Could not check balance: {resp.status_code}")
            return

        balance = float(resp.json().get("credits", 0))
        if balance >= min_credits:
            print(f"[Credits] Balance {balance:.1f} >= {min_credits:.1f}, OK")
            return

        shortfall = min_credits - balance
        user_id = _get_user_id_from_key()
        if not user_id:
            print("[Credits] Could not resolve user_id for top-up")
            return

        top_up = requests.post(
            f"{ORCHESTRA_URL}/admin/create_recharge",
            json={
                "user_id": user_id,
                "quantity": shortfall,
                "type": "promo",
            },
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=10,
        )
        if top_up.status_code in (200, 201):
            print(
                f"[Credits] Topped up {shortfall:.1f} credits "
                f"(was {balance:.1f}, need {min_credits:.1f})",
            )
        else:
            print(
                f"[Credits] Top-up failed: {top_up.status_code} {top_up.text}",
            )
    except Exception as e:
        print(f"[Credits] Error during credit check/top-up: {e}")


# ---------------------------------------------------------------------------
# Assistant factory helpers
# ---------------------------------------------------------------------------

ASSISTANT_CREATION_COST = 10.0


def _admin_record_to_data(a: dict) -> dict:
    """Convert an admin assistant record to the dict shape expected by
    start_real_job and other test helpers.

    This is the canonical mapping used by both real_assistant_data and
    the test assistant factory.
    """
    return {
        "assistant_id": a["agent_id"],
        "user_id": a["user_id"],
        "api_key": a["api_key"],
        "user_first_name": a["user_first_name"],
        "user_surname": a.get("user_last_name", ""),
        "user_email": a["user_email"],
        "assistant_first_name": a["first_name"],
        "assistant_surname": a["surname"],
        "assistant_age": str(a.get("age", "")),
        "assistant_nationality": a["nationality"],
        "assistant_about": a["about"],
        "assistant_timezone": a.get("timezone", "UTC"),
        "assistant_number": a.get("phone") or "",
        "assistant_email": a.get("email") or "",
        "user_number": a.get("user_phone") or "",
        "user_whatsapp_number": a.get("user_whatsapp_number") or "",
        "voice_provider": a["voice_provider"],
        "voice_id": a["voice_id"],
        "desktop_mode": a.get("desktop_mode", "ubuntu"),
        "user_desktop_mode": a.get("user_desktop_mode") or "",
        "user_desktop_filesys_sync": str(
            a.get("user_desktop_filesys_sync", False),
        ).lower(),
        "user_desktop_url": a.get("user_desktop_url") or "",
        "demo_id": "",
        "team_ids": json.dumps(a.get("team_ids", [])),
        "org_id": (
            str(a.get("organization_id", "")) if a.get("organization_id") else ""
        ),
    }


def _create_test_assistant(index: int) -> dict:
    """Create a test assistant on Orchestra and return its full data.

    Calls POST /v0/assistant with is_local=True (skips wakeup) and
    create_infra=True (provisions Pub/Sub topic).  Then fetches the full
    admin record to get api_key and user fields.
    """
    assert UNIFY_KEY, "UNIFY_KEY required to create test assistants"
    assert ADMIN_KEY, "ORCHESTRA_ADMIN_KEY required to fetch admin records"

    payload = {
        "first_name": "InfraTest",
        "surname": f"{index:03d}",
        "age": 25,
        "nationality": "North America",
        "about": "Stress test assistant (auto-created by integration tests)",
        "desktop_mode": "ubuntu",
        "is_local": True,
        "create_infra": True,
        "timezone": "UTC",
    }
    if NAMESPACE == "preview":
        payload["deploy_env"] = "preview"

    create_resp = requests.post(
        f"{ORCHESTRA_URL}/assistant",
        json=payload,
        headers={"Authorization": f"Bearer {UNIFY_KEY}"},
        timeout=90,
    )
    assert create_resp.status_code == 200, (
        f"Failed to create test assistant {index}: "
        f"{create_resp.status_code} {create_resp.text}"
    )

    info = create_resp.json().get("info", {})
    agent_id = str(info.get("agent_id", ""))
    assert agent_id, (
        f"No agent_id in create response for assistant {index}: " f"{create_resp.text}"
    )

    admin_resp = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params={"agent_id": agent_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=10,
    )
    assert admin_resp.status_code == 200, (
        f"Failed to fetch admin record for {agent_id}: "
        f"{admin_resp.status_code} {admin_resp.text}"
    )

    admin_info = admin_resp.json()["info"]
    a = admin_info[0] if isinstance(admin_info, list) else admin_info
    return _admin_record_to_data(a)


def _create_preview_managed_assistant() -> dict:
    """Create a non-local preview-routed assistant for preview E2E flows."""
    assert UNIFY_KEY, "UNIFY_KEY required to create preview assistants"
    assert ADMIN_KEY, "ORCHESTRA_ADMIN_KEY required to fetch admin records"

    payload = {
        "first_name": "PreviewInfra",
        "surname": f"{int(time.time()) % 100000:05d}",
        "age": 25,
        "nationality": "North America",
        "about": "Preview integration assistant (auto-created by tests)",
        "desktop_mode": "ubuntu",
        "is_local": False,
        "create_infra": True,
        "deploy_env": "preview",
        "timezone": "UTC",
    }

    create_resp = requests.post(
        f"{ORCHESTRA_URL}/assistant",
        json=payload,
        headers={"Authorization": f"Bearer {UNIFY_KEY}"},
        timeout=90,
    )
    assert (
        create_resp.status_code == 200
    ), f"Failed to create preview assistant: {create_resp.status_code} {create_resp.text}"

    info = create_resp.json().get("info", {})
    agent_id = str(info.get("agent_id", ""))
    assert (
        agent_id
    ), f"No agent_id in preview assistant create response: {create_resp.text}"

    admin_resp = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params={"agent_id": agent_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=10,
    )
    assert admin_resp.status_code == 200, (
        f"Failed to fetch admin record for preview assistant {agent_id}: "
        f"{admin_resp.status_code} {admin_resp.text}"
    )

    admin_info = admin_resp.json()["info"]
    a = admin_info[0] if isinstance(admin_info, list) else admin_info
    return _admin_record_to_data(a)


def _delete_test_assistant(agent_id: str, batch_api=None):
    """Delete a test assistant from Orchestra and clean up infra resources.

    Calls DELETE /v0/assistant/{id} which handles Pub/Sub, disks, phones,
    emails, and DB cleanup.  Also expires any AssistantJobs records and
    deletes K8s Jobs.

    Swallows all exceptions so teardown never aborts mid-way.
    """
    int_id = str(agent_id).split(".")[0]

    try:
        requests.post(
            f"{COMMS_APP_URL}/infra/vm/pool/release",
            json={"assistant_id": str(agent_id)},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
    except Exception:
        pass

    try:
        resp = requests.delete(
            f"{ORCHESTRA_URL}/assistant/{int_id}",
            headers={"Authorization": f"Bearer {UNIFY_KEY}"},
            timeout=30,
        )
        if resp.status_code == 200:
            print(f"[Teardown] Deleted assistant {agent_id}")
        else:
            print(
                f"[Teardown] DELETE assistant {agent_id}: "
                f"{resp.status_code} {resp.text}",
            )
    except Exception as e:
        print(f"[Teardown] Error deleting assistant {agent_id}: {e}")

    try:
        expire_test_assistant_records(str(agent_id))
    except Exception:
        pass

    if batch_api is not None:
        sanitized = str(agent_id).lower().replace("_", "-")
        try:
            jobs = batch_api.list_namespaced_job(
                namespace=NAMESPACE,
                label_selector=f"app=unity,assistant-id={sanitized}",
            )
            for job in jobs.items:
                try:
                    batch_api.delete_namespaced_job(
                        name=job.metadata.name,
                        namespace=NAMESPACE,
                        propagation_policy="Foreground",
                    )
                except Exception:
                    pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Test assistant factory fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_assistants(k8s_clients):
    """Create N test assistants on Orchestra for stress testing.

    Controlled by TEST_CREATE_ASSISTANT_COUNT env var (default 0 = skip).
    Assistants are created with is_local=True (no wakeup / auto-start)
    and create_infra=True (Pub/Sub topics provisioned).

    Yields a list of dicts in the same shape as real_assistant_data.
    On teardown, all assistants are deleted via DELETE /v0/assistant/{id}.
    """
    count = int(os.getenv("TEST_CREATE_ASSISTANT_COUNT", "0"))
    if count == 0:
        yield []
        return

    assert UNIFY_KEY, (
        "UNIFY_KEY required to create test assistants "
        "(set TEST_CREATE_ASSISTANT_COUNT=0 to skip)"
    )
    assert ADMIN_KEY, (
        "ORCHESTRA_ADMIN_KEY required to create test assistants "
        "(set TEST_CREATE_ASSISTANT_COUNT=0 to skip)"
    )

    _ensure_credits(min_credits=count * (ASSISTANT_CREATION_COST + 5))

    created: list[dict] = []
    for i in range(count):
        try:
            data = _create_test_assistant(i)
            created.append(data)
            print(
                f"[Factory] Created assistant {i + 1}/{count}: "
                f"{data['assistant_first_name']} {data['assistant_surname']} "
                f"(ID {data['assistant_id']})",
            )
            if i < count - 1:
                time.sleep(0.5)
        except Exception as e:
            print(f"[Factory] Failed to create assistant {i}: {e}")

    print(f"\n[Factory] Created {len(created)}/{count} test assistants")
    yield created

    batch_api = k8s_clients[0]
    print(f"\n[Teardown] Deleting {len(created)} test assistants...")
    for a in created:
        _delete_test_assistant(a["assistant_id"], batch_api)
    if created:
        replenish_pool()
    print(f"[Teardown] Done")


# ---------------------------------------------------------------------------
# Stress test helpers: adapter callers
# ---------------------------------------------------------------------------

_PUBSUB_SUFFIX = os.getenv(
    "TEST_PUBSUB_SUFFIX",
    f"-{NAMESPACE}" if NAMESPACE != "production" else "",
)


def send_test_message(assistant_data: dict, body: str = "Integration test message"):
    """Send a message via the adapter's /unify/message endpoint."""
    resp = requests.post(
        f"{ADAPTERS_URL}/unify/message",
        json={
            "assistant_id": str(assistant_data["assistant_id"]),
            "contact_id": 1,
            "body": body,
        },
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    return resp


def send_test_meet(assistant_data: dict, room_name: str | None = None):
    """Send a meet invite via the adapter's /unify/meet endpoint."""
    aid = str(assistant_data["assistant_id"])
    room = room_name or f"stress-test-meet-{aid}-{int(time.time())}"
    resp = requests.post(
        f"{ADAPTERS_URL}/unify/meet",
        json={
            "assistant_id": aid,
            "room_name": room,
            "livekit_agent_name": f"unity_{aid}",
        },
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    return resp


def send_test_system_event(
    assistant_data: dict,
    event_type: str,
    message: str = "",
):
    """Send a system event via the adapter's /unity/system-event endpoint."""
    resp = requests.post(
        f"{ADAPTERS_URL}/unity/system-event",
        json={
            "assistant_id": str(assistant_data["assistant_id"]),
            "event_type": event_type,
            "message": message or f"Stress test: {event_type}",
        },
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    return resp


# ---------------------------------------------------------------------------
# Channel test helpers: Twilio signature, assistant lookups
# ---------------------------------------------------------------------------


def _fetch_secret(secret_name: str) -> str | None:
    """Fetch a secret from GCP Secret Manager. Returns None on failure."""
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{GCP_PROJECT_ID}/secrets/{secret_name}/versions/latest"
        return client.access_secret_version(
            request={"name": name},
        ).payload.data.decode("utf-8")
    except Exception:
        return None


@pytest.fixture(scope="session")
def twilio_auth_token():
    """Fetch TWILIO_AUTH_TOKEN from Secret Manager for signature computation."""
    token = os.getenv("TWILIO_AUTH_TOKEN") or _fetch_secret("TWILIO_AUTH_TOKEN")
    if not token:
        pytest.skip("TWILIO_AUTH_TOKEN not available")
    return token


@pytest.fixture(scope="session")
def livekit_credentials():
    """Fetch LiveKit API credentials for webhook signature computation."""
    api_key = os.getenv("LIVEKIT_API_KEY") or _fetch_secret("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET") or _fetch_secret("LIVEKIT_API_SECRET")
    if not api_key or not api_secret:
        pytest.skip("LIVEKIT_API_KEY / LIVEKIT_API_SECRET not available")
    return {"api_key": api_key, "api_secret": api_secret}


def compute_twilio_signature(url: str, params: dict, auth_token: str) -> str:
    """Compute a valid X-Twilio-Signature for the given URL and form params."""
    from twilio.request_validator import RequestValidator

    return RequestValidator(auth_token).compute_signature(url, params)


def _fetch_all_user_assistants() -> list[dict]:
    """Fetch all assistants for the current user via Orchestra."""
    resp = requests.get(
        f"{ORCHESTRA_URL}/assistant",
        headers={"Authorization": f"Bearer {UNIFY_KEY}"},
        timeout=30,
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("info", [])


def find_assistant_with_phone() -> dict | None:
    """Find a user assistant that has a Twilio phone number assigned."""
    for a in _fetch_all_user_assistants():
        if a.get("phone"):
            return a
    return None


def find_assistant_with_email() -> dict | None:
    """Find a user assistant that has an email address assigned."""
    for a in _fetch_all_user_assistants():
        if a.get("email"):
            return a
    return None


def create_and_cleanup_idle_job(comms_client) -> str:
    """Create an idle job and return its name. Caller must delete it."""
    resp = comms_client.post("/infra/job/create")
    assert resp.status_code == 200, f"job/create failed: {resp.status_code} {resp.text}"
    return resp.json()["job_name"]


def find_assistant_with_assigned_vm() -> dict | None:
    """Find a user assistant that has a running container and an assigned VM."""
    try:
        from google.cloud import compute_v1

        client = compute_v1.InstancesClient()
        from common.settings import SETTINGS

        request = compute_v1.ListInstancesRequest(
            project=SETTINGS.vm_project_id,
            zone=SETTINGS.vm_zone,
            filter="labels.pool-role=assigned",
        )
        vms = list(client.list(request=request))
        if not vms:
            return None
        aid = vms[0].labels.get("assistant-id")
        if not aid:
            return None
        resp = requests.get(
            f"{ORCHESTRA_URL}/admin/assistant",
            params={"agent_id": aid},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=10,
        )
        assistants = resp.json().get("info", [])
        if assistants:
            return {
                "assistant_id": assistants[0]["agent_id"],
                "vm_name": vms[0].name,
                **{k: v for k, v in assistants[0].items() if k not in ("agent_id",)},
            }
    except Exception:
        pass
    return None


def generate_vm_identity_token() -> str | None:
    """Generate a GCP identity token impersonating pool-vm-sa.

    Uses the comm-sa service account key to impersonate the pool VM SA
    with audience 'unity-comms-vm'. Returns None if impersonation fails
    (missing IAM permissions).
    """
    try:
        import google.auth.transport.requests
        import google.oauth2.service_account
        from google.auth import impersonated_credentials

        sa_key_json = _fetch_secret("gcp-sa-key")
        if not sa_key_json:
            return None
        sa_key = json.loads(sa_key_json)

        source_creds = (
            google.oauth2.service_account.Credentials.from_service_account_info(
                sa_key,
            )
        )
        target_creds = impersonated_credentials.IDTokenCredentials(
            target_credentials=impersonated_credentials.Credentials(
                source_credentials=source_creds,
                target_principal="service-account@example.iam.gserviceaccount.com",
                target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
            ),
            target_audience="unity-comms-vm",
        )
        request = google.auth.transport.requests.Request()
        target_creds.refresh(request)
        return target_creds.token
    except Exception:
        return None


def compute_livekit_webhook_auth(body: str, api_key: str, api_secret: str) -> str:
    """Compute a LiveKit webhook Authorization header (Bearer JWT)."""
    import hashlib
    import jwt as pyjwt

    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    token = pyjwt.encode(
        {"sha256": body_hash, "sub": api_key},
        api_secret,
        algorithm="HS256",
    )
    return token


# ---------------------------------------------------------------------------
# Stress test helpers: direct Pub/Sub
# ---------------------------------------------------------------------------


def publish_to_assistant_topic(
    publisher,
    assistant_id: str,
    thread: str,
    event: dict,
):
    """Publish a message directly to an assistant's Pub/Sub topic.

    Bypasses the adapter entirely — used to simulate SMS, email, Teams
    inbound without external service credentials.
    """
    topic_name = f"unity-{assistant_id}{_PUBSUB_SUFFIX}"
    topic_path = publisher.topic_path(GCP_PROJECT_ID, topic_name)
    data = json.dumps(
        {
            "thread": thread,
            "publish_timestamp": time.time(),
            "event": event,
        },
    ).encode("utf-8")
    future = publisher.publish(topic_path, data=data)
    return future.result(timeout=10)


def pull_outbound_messages(
    subscriber,
    assistant_id: str,
    max_messages: int = 10,
    timeout: float = 10,
) -> list[dict]:
    """Pull messages from an assistant's outbound Pub/Sub subscription.

    Returns a list of parsed event dicts (may be empty if no messages).
    Acknowledges all pulled messages.
    """
    from google.api_core.exceptions import DeadlineExceeded

    sub_name = f"unity-{assistant_id}{_PUBSUB_SUFFIX}-outbound-sub"
    sub_path = subscriber.subscription_path(GCP_PROJECT_ID, sub_name)
    try:
        response = subscriber.pull(
            request={"subscription": sub_path, "max_messages": max_messages},
            timeout=timeout,
        )
    except DeadlineExceeded:
        return []

    messages = response.received_messages
    if not messages:
        return []

    ack_ids = [m.ack_id for m in messages]
    subscriber.acknowledge(
        request={"subscription": sub_path, "ack_ids": ack_ids},
    )

    results = []
    for m in messages:
        try:
            results.append(json.loads(m.message.data.decode("utf-8")))
        except Exception:
            pass
    return results


# ---------------------------------------------------------------------------
# Stress test helpers: K8s container polling
# ---------------------------------------------------------------------------


def wait_for_container_running(
    batch_api,
    assistant_id: str,
    timeout: float = 180,
    interval: float = 10,
) -> list:
    """Poll until a Job with assistant-id={id} and active pods appears."""
    return poll_until(
        lambda: list_jobs_with_assistant_id(batch_api, str(assistant_id)),
        timeout=timeout,
        interval=interval,
        description=f"Container for assistant {assistant_id} to start",
        failure_snapshot=lambda: describe_runtime_state(
            batch_api,
            None,
            None,
            str(assistant_id),
        ),
    )


def wait_for_container_done(
    batch_api,
    assistant_id: str,
    timeout: float = 540,
    interval: float = 15,
):
    """Poll until no active Jobs exist for this assistant."""

    def _check():
        jobs = list_jobs_with_assistant_id(batch_api, str(assistant_id))
        return len(jobs) == 0

    poll_until(
        _check,
        timeout=timeout,
        interval=interval,
        description=f"Container for assistant {assistant_id} to shut down",
        failure_snapshot=lambda: describe_runtime_state(
            batch_api,
            None,
            None,
            str(assistant_id),
        ),
    )


# ---------------------------------------------------------------------------
# Stress test helpers: VM probes
# ---------------------------------------------------------------------------


def probe_vm_https(hostname: str, timeout: float = 5.0) -> bool:
    """Check if the agent-service is alive behind Caddy.

    Hits ``/api/sessions`` through the Caddy reverse proxy.  A 401 from
    the agent-service auth middleware proves it is running; a 502 from
    Caddy or a connection error means the VM is broken.
    """
    try:
        r = requests.get(
            f"https://{hostname}/api/sessions",
            timeout=timeout,
            verify=False,
        )
        return r.status_code < 500
    except requests.RequestException:
        return False


def probe_vm_agent_service(
    hostname: str,
    api_key: str,
    command: str = "echo ok",
) -> requests.Response | None:
    """Call /api/exec on the VM's agent-service. Returns the response or None."""
    try:
        return requests.post(
            f"https://{hostname}/api/exec",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"command": command, "timeout": 5000},
            timeout=10,
            verify=False,
        )
    except requests.RequestException:
        return None


def probe_vm_ssh_port(hostname: str, port: int = 2222, timeout: float = 5.0) -> bool:
    """Check if the SSH sync port is open on the VM."""
    import socket

    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


# ---------------------------------------------------------------------------
# GCE VM helpers
# ---------------------------------------------------------------------------


def list_assigned_vms(gce_client, assistant_id: str) -> list:
    """List GCE VMs assigned to this assistant."""
    from google.cloud import compute_v1

    sanitized = assistant_id.lower().replace("_", "-")
    request = compute_v1.ListInstancesRequest(
        project=VM_PROJECT_ID,
        zone=VM_ZONE,
        filter=f"labels.pool-role=assigned AND labels.assistant-id={sanitized}",
    )
    return list(gce_client.list(request=request))


def list_idle_vms(gce_client, vm_type: str = "ubuntu") -> list:
    """List idle pool VMs."""
    from google.cloud import compute_v1

    request = compute_v1.ListInstancesRequest(
        project=VM_PROJECT_ID,
        zone=VM_ZONE,
        filter=f"labels.pool-role=idle AND labels.vm-type={vm_type} AND status=RUNNING",
    )
    return list(gce_client.list(request=request))


def list_stopped_vms(gce_client, vm_type: str = "ubuntu") -> list:
    """List stopped (TERMINATED) pool VMs."""
    from google.cloud import compute_v1

    request = compute_v1.ListInstancesRequest(
        project=VM_PROJECT_ID,
        zone=VM_ZONE,
        filter=f"labels.pool-role=stopped AND labels.vm-type={vm_type} AND status=TERMINATED",
    )
    return list(gce_client.list(request=request))


def list_ghost_vms(gce_client, vm_type: str = "ubuntu") -> list:
    """List VMs with label/status mismatches that the scrub function fixes.

    Returns VMs matching any of the anomaly patterns:
      stopped + RUNNING, idle + TERMINATED, idle + SUSPENDED,
      starting + TERMINATED, provisioning + TERMINATED,
      assigned + TERMINATED, quarantined + RUNNING.
    """
    from google.cloud import compute_v1

    _ANOMALIES = {
        ("stopped", "RUNNING"),
        ("idle", "TERMINATED"),
        ("idle", "SUSPENDED"),
        ("starting", "TERMINATED"),
        ("provisioning", "TERMINATED"),
        ("assigned", "TERMINATED"),
        ("quarantined", "RUNNING"),
    }

    request = compute_v1.ListInstancesRequest(
        project=VM_PROJECT_ID,
        zone=VM_ZONE,
        filter=f"labels.vm-type={vm_type}",
    )
    return [
        vm
        for vm in gce_client.list(request=request)
        if vm.labels and (vm.labels.get("pool-role", ""), vm.status) in _ANOMALIES
    ]


# ---------------------------------------------------------------------------
# Invariant checker
# ---------------------------------------------------------------------------


@dataclass
class InvariantViolation:
    invariant_id: str
    message: str


def check_invariants(batch_api, gce_client=None) -> list[InvariantViolation]:
    """Run invariant checks against current deployed state.

    Returns a list of violations (empty = all clear).
    GCE checks are skipped if gce_client is None.
    """
    violations = []

    jobs = batch_api.list_namespaced_job(
        namespace=NAMESPACE,
        label_selector="app=unity",
    ).items

    assistant_jobs_map = {}
    idle_count = 0

    for job in jobs:
        labels = job.metadata.labels or {}
        status = labels.get("unity-status", "")
        aid = labels.get("assistant-id", "")
        has_active = job.status.active and job.status.active > 0

        if not has_active:
            continue

        if status == "idle" and not aid:
            idle_count += 1

        if aid:
            if aid in assistant_jobs_map:
                violations.append(
                    InvariantViolation(
                        "INV-1",
                        f"Duplicate: assistant {aid} has Jobs {assistant_jobs_map[aid]} and {job.metadata.name}",
                    ),
                )
            assistant_jobs_map[aid] = job.metadata.name

            if status not in ("running", "live", "done"):
                violations.append(
                    InvariantViolation(
                        "INV-2",
                        f"Job {job.metadata.name} has assistant-id={aid} but unity-status={status}",
                    ),
                )

        if status == "idle" and aid:
            violations.append(
                InvariantViolation(
                    "INV-3",
                    f"Idle Job {job.metadata.name} has assistant-id={aid} (should be empty)",
                ),
            )

    if idle_count < 1 and assistant_jobs_map:
        violations.append(
            InvariantViolation(
                "INV-5",
                f"Pool exhausted: {len(assistant_jobs_map)} live containers but {idle_count} idle",
            ),
        )

    if gce_client is not None:
        try:
            from google.cloud import compute_v1

            request = compute_v1.ListInstancesRequest(
                project=VM_PROJECT_ID,
                zone=VM_ZONE,
                filter="labels.pool-role=assigned",
            )
            assigned_vms = list(gce_client.list(request=request))

            for vm in assigned_vms:
                vm_aid = (vm.labels or {}).get("assistant-id", "")
                if vm_aid and vm_aid not in assistant_jobs_map:
                    violations.append(
                        InvariantViolation(
                            "INV-10",
                            f"VM {vm.name} assigned to {vm_aid} but no running K8s Job found",
                        ),
                    )

            idle_vm_count = len(list_idle_vms(gce_client))
            if idle_vm_count < 1:
                violations.append(
                    InvariantViolation(
                        "INV-12",
                        f"VM pool exhausted: {idle_vm_count} idle VMs",
                    ),
                )
        except Exception as e:
            violations.append(InvariantViolation("GCE", f"GCE check failed: {e}"))

    return violations


@pytest.fixture(scope="session")
def invariant_baseline(k8s_clients, gce_client):
    """Snapshot invariant violations before the test suite runs."""
    batch_api = k8s_clients[0]
    return check_invariants(batch_api, gce_client)


@pytest.fixture(autouse=True)
def check_invariants_after_test(request, k8s_clients, gce_client, invariant_baseline):
    """After each test, check for NEW invariant violations.

    Reports violations as warnings rather than failing the test, because
    violations from previous tests (e.g., duplicate containers from the
    split-brain test) can cascade and cause false failures on unrelated tests.
    The invariant checker test (test_invariant_checker.py) is the authoritative
    place for invariant assertions.
    """
    yield
    batch_api = k8s_clients[0]
    core_api = k8s_clients[1]
    current = check_invariants(batch_api, gce_client)
    baseline_ids = {(v.invariant_id, v.message) for v in invariant_baseline}
    new_violations = [
        v for v in current if (v.invariant_id, v.message) not in baseline_ids
    ]
    failed = bool(
        getattr(request.node, "rep_call", None) and request.node.rep_call.failed,
    )
    artifact_path = None
    if failed or new_violations:
        try:
            artifact_path = _build_failure_artifact(
                request,
                batch_api,
                core_api,
                gce_client,
                new_violations,
            )
            print(f"\n[Failure Artifact] {artifact_path}")
        except Exception as exc:
            artifact_path = f"artifact generation failed: {type(exc).__name__}: {exc}"
    if new_violations:
        msg = "New invariant violations after test:\n"
        for v in new_violations:
            msg += f"  [{v.invariant_id}] {v.message}\n"
        assistant_ids = sorted(
            set(_assistant_ids_from_request(request, batch_api))
            | set(_assistant_ids_from_violation_messages(new_violations)),
        )
        for assistant_id in assistant_ids:
            runtime = describe_runtime_state(
                batch_api,
                core_api,
                gce_client,
                assistant_id,
            )
            session = runtime.get("session") or {}
            status = session.get("status") or {}
            spec = session.get("spec") or {}
            msg += (
                "  [SESSION] "
                f"assistant={assistant_id} "
                f"session={(session.get('metadata') or {}).get('name')} "
                f"activation={spec.get('activationId')} "
                f"phase={status.get('phase')} "
                f"jobRef={(status.get('jobRef') or {}).get('name')} "
                f"vmRef={(status.get('vmRef') or {}).get('name')} "
                f"lastError={status.get('lastError')}\n"
            )
        if artifact_path:
            msg += f"  [ARTIFACT] {artifact_path}\n"
        import warnings

        warnings.warn(msg)
