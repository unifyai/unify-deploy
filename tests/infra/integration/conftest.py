"""
Shared fixtures for infrastructure integration tests.

These tests run against real staging K8s and GCE infrastructure.
Every test creates its own resources and cleans up in finally blocks.

Configuration:
    Copy tests/infra/integration/.env.example to .env and fill in your
    credentials. The .env file is gitignored. See README.md for details.
"""

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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
# Staging configuration (all overridable via env vars)
# ---------------------------------------------------------------------------

GCP_PROJECT_ID = os.getenv("TEST_GCP_PROJECT_ID", "gcp-project-runtime")
VM_PROJECT_ID = os.getenv("TEST_VM_PROJECT_ID", "gcp-project-vms")
GKE_CLUSTER = os.getenv("TEST_GKE_CLUSTER", "unity")
GKE_REGION = os.getenv("TEST_GKE_REGION", "us-central1")
NAMESPACE = os.getenv("TEST_NAMESPACE", "staging")
PUBSUB_STARTUP_TOPIC = os.getenv("TEST_PUBSUB_STARTUP_TOPIC", "unity-startup-staging")
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
        "staging: tests that run against real staging infrastructure",
    )
    config.addinivalue_line(
        "markers",
        "invariant: which INV-N invariant(s) the test covers",
    )


# ---------------------------------------------------------------------------
# Test ID generation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def real_assistant_data():
    """Fetch a real staging assistant's full data from Orchestra.

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
        ), "No assistants found for this user. Hire one on staging first."
        assistant_id = str(assistants[0]["agent_id"])
        print(
            f"\n[Setup] Auto-detected assistant: {assistants[0].get('first_name', '')} "
            f"{assistants[0].get('surname', '')} (ID {assistant_id})",
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
        "org_id": str(a.get("organization_id", "")) if a.get("organization_id") else "",
    }


@pytest.fixture
def test_id(real_assistant_data):
    """The real assistant ID from staging Orchestra."""
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
    """Authenticated K8s BatchV1Api and CoreV1Api for the staging cluster."""
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
    import subprocess

    try:
        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"],
            text=True,
            timeout=10,
        ).strip()

        from google.oauth2 import credentials as oauth2_credentials

        creds = oauth2_credentials.Credentials(token=token)
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
) -> Any:
    """Poll until condition() returns a truthy value, or raise on timeout."""
    deadline = time.monotonic() + timeout
    last_result = None
    while time.monotonic() < deadline:
        last_result = condition()
        if last_result:
            return last_result
        time.sleep(interval)
    raise TimeoutError(
        f"Timed out after {timeout}s waiting for {description}. "
        f"Last result: {last_result}",
    )


@pytest.fixture
def poll():
    """Polling helper fixture."""
    return poll_until


# ---------------------------------------------------------------------------
# Comms App HTTP helpers
# ---------------------------------------------------------------------------


@dataclass
class CommsClient:
    """HTTP client for the staging Comms App."""

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
    """HTTP client for the staging Adapters service."""

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


def replenish_staging_pool():
    """Trigger idle pool replenishment on staging adapters.

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
            replenish_staging_pool()


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
        replenish_staging_pool()
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


# ---------------------------------------------------------------------------
# Invariant checker
# ---------------------------------------------------------------------------


@dataclass
class InvariantViolation:
    invariant_id: str
    message: str


def check_invariants(batch_api, gce_client=None) -> list[InvariantViolation]:
    """Run invariant checks against current staging state.

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
    current = check_invariants(batch_api, gce_client)
    baseline_ids = {(v.invariant_id, v.message) for v in invariant_baseline}
    new_violations = [
        v for v in current if (v.invariant_id, v.message) not in baseline_ids
    ]
    if new_violations:
        msg = "New invariant violations after test:\n"
        for v in new_violations:
            msg += f"  [{v.invariant_id}] {v.message}\n"
        import warnings

        warnings.warn(msg)
