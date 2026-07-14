"""
Shared fixtures for infrastructure integration tests.

These tests run against real deployed K8s and GCE infrastructure.
Every test creates its own resources and cleans up in finally blocks.

Configuration:
    Copy tests/infra/integration/.env.example to .env and fill in your
    credentials. The .env file is gitignored. See README.md for details.
"""

import builtins
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import pytest
import requests
from dotenv import load_dotenv

from tests.communication.infra.integration.pubsub_auth import (
    build_pubsub_publisher_client,
    build_pubsub_subscriber_client,
    client_credential_context,
    resolve_pubsub_credentials,
)

_CURRENT_RUNTIME_IDENTITY_TRACKER = None
_INTEGRATION_LOG_STARTED_AT_MONOTONIC = time.monotonic()

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


def _integration_log_prefix() -> str:
    """Return the standard prefix for integration-test console output."""

    wall_clock = datetime.now(UTC).strftime("%H:%M:%S")
    elapsed_seconds = time.monotonic() - _INTEGRATION_LOG_STARTED_AT_MONOTONIC
    return f"[{wall_clock} +{elapsed_seconds:7.1f}s]"


def _prefix_integration_log_lines(message: str) -> str:
    """Prefix each non-empty line while preserving blank separator lines."""

    if not message:
        return ""

    prefixed_lines: list[str] = []
    for line in message.splitlines(keepends=True):
        line_ending = "\n" if line.endswith("\n") else ""
        line_body = line[:-1] if line_ending else line
        if line_body:
            prefixed_lines.append(
                f"{_integration_log_prefix()} {line_body}{line_ending}",
            )
        else:
            prefixed_lines.append(line_ending)
    return "".join(prefixed_lines)


def integration_print(*args, sep: str = " ", end: str = "\n", **kwargs) -> None:
    """Print integration-test progress with wall-clock and elapsed timestamps."""

    if not args:
        builtins.print(*args, sep=sep, end=end, flush=True, **kwargs)
        return

    message = sep.join(str(arg) for arg in args)
    builtins.print(
        _prefix_integration_log_lines(message),
        end=end,
        flush=True,
        **kwargs,
    )


print = integration_print

# ---------------------------------------------------------------------------
# Deployment configuration (all overridable via env vars)
# ---------------------------------------------------------------------------

GCP_PROJECT_ID = os.getenv("TEST_GCP_PROJECT_ID", "gcp-project-runtime")
VM_PROJECT_ID = os.getenv("TEST_VM_PROJECT_ID", "gcp-project-vms")
GKE_CLUSTER = os.getenv("TEST_GKE_CLUSTER", "unity")
GKE_REGION = os.getenv("TEST_GKE_REGION", "us-central1")
NAMESPACE = os.getenv("TEST_NAMESPACE", "staging")
VM_ZONE = os.getenv("TEST_VM_ZONE", "us-central1-a")
ASSISTANT_SESSION_REF_LABEL = "assistantsession.unify.ai/name"
ASSISTANT_SESSION_REF_ANNOTATION = "assistantsession.unify.ai/name"
ASSISTANT_SESSION_BINDING_LABEL = "assistantsession.unify.ai/binding-id"

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

LOCAL_COMMUNICATION_CONFIG = Path(
    os.getenv(
        "TEST_COMMUNICATION_LOCAL_CONFIG",
        "/tmp/communication-local.config",
    ),
)
DEFAULT_LOCAL_ORCHESTRA_URL = "http://localhost:8000/v0"
DEFAULT_LOCAL_ADAPTERS_URL = "http://127.0.0.1:8081"
DEFAULT_LOCAL_COMMS_URL = "http://127.0.0.1:8082"
DEFAULT_LOCAL_PUBSUB_HOST = "localhost:8085"
DEFAULT_LOCAL_GCP_PROJECT_ID = "local-test-project"
DEFAULT_LOCAL_ADMIN_KEY = "local-admin-key"
LOCAL_STACK_START_TIMEOUT_SECONDS = 600
LOCAL_STACK_WAIT_TIMEOUT_SECONDS = 300
SELF_HOST_BOOTSTRAP_PATH = Path("/tmp/self-host-bootstrap.json")
SELF_HOST_CREDENTIALS_PATH = Path(
    os.getenv(
        "SELF_HOST_CREDENTIALS_FILE",
        str(Path.home() / ".unity" / "self-host-credentials.json"),
    ),
)


@dataclass(frozen=True)
class LocalStackUrls:
    """Resolved service URLs for the local self-host / stack.sh environment."""

    orchestra_url: str
    adapters_url: str
    comms_url: str
    pubsub_emulator_host: str
    gcp_project_id: str
    pubsub_suffix: str


def _parse_communication_local_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _local_stack_service_reachable(url: str, *, api_key: str) -> bool:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    for path in ("", "/health", "/healthz"):
        try:
            response = requests.get(
                f"{url.rstrip('/')}{path}",
                headers=headers,
                timeout=3,
            )
            if response.status_code < 500:
                return True
        except requests.RequestException:
            continue
    return False


def _orchestra_reachable(orchestra_url: str, *, api_key: str) -> bool:
    try:
        response = requests.get(
            f"{orchestra_url.rstrip('/')}/user/basic-info",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5,
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def _resolve_unify_root() -> Path:
    configured = os.getenv("UNIFY_STACK_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Unify"


def _resolve_sibling_repo(name: str) -> Path:
    env_key = f"{name.upper()}_REPO_PATH"
    configured = os.getenv(env_key, "").strip()
    if configured:
        return Path(configured).expanduser()
    unify_root = _resolve_unify_root()
    for candidate in (f"{name}-teams-unify", name):
        path = unify_root / candidate
        if path.is_dir():
            return path
    return unify_root / name


def _resolve_stack_script() -> tuple[Path, Path]:
    """Return (cwd, stack.sh path).

    The self-host stack orchestration lives in this repo under
    ``selfhost/stack.sh`` and drives the sibling unify/console/orchestra
    checkouts.
    """

    deploy_repo = Path(__file__).resolve().parents[4]
    stack_script = deploy_repo / "selfhost" / "stack.sh"
    if not stack_script.is_file():
        raise FileNotFoundError(f"stack.sh not found at {stack_script}")
    return deploy_repo, stack_script


def _load_self_host_bootstrap_credentials() -> dict[str, Any]:
    for path in (SELF_HOST_CREDENTIALS_PATH, SELF_HOST_BOOTSTRAP_PATH):
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _apply_local_stack_credentials(*, unify_key: str, admin_key: str) -> None:
    """Publish local stack credentials to env and module-level constants."""

    global ADMIN_KEY, UNIFY_KEY

    os.environ["UNIFY_KEY"] = unify_key
    os.environ["ORCHESTRA_ADMIN_KEY"] = admin_key
    UNIFY_KEY = unify_key
    ADMIN_KEY = admin_key


def _resolve_local_stack_credentials() -> tuple[str, str]:
    bootstrap = _load_self_host_bootstrap_credentials()
    unify_key = os.getenv("UNIFY_KEY", bootstrap.get("api_key", "")).strip()
    admin_key = os.getenv("ORCHESTRA_ADMIN_KEY", DEFAULT_LOCAL_ADMIN_KEY).strip()
    return unify_key, admin_key


def _local_stack_is_ready(
    urls: LocalStackUrls,
    *,
    unify_key: str,
    admin_key: str,
) -> bool:
    if not unify_key or not admin_key:
        return False
    if not _orchestra_reachable(urls.orchestra_url, api_key=unify_key):
        return False
    if not _local_stack_service_reachable(urls.adapters_url, api_key=admin_key):
        return False
    if not _local_stack_service_reachable(urls.comms_url, api_key=admin_key):
        return False
    return True


def _wait_for_local_stack(
    urls: LocalStackUrls,
    *,
    unify_key: str,
    admin_key: str,
    timeout_seconds: int = LOCAL_STACK_WAIT_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _local_stack_is_ready(
            urls,
            unify_key=unify_key,
            admin_key=admin_key,
        ):
            return
        time.sleep(2)
    raise TimeoutError(
        "Timed out waiting for local stack at "
        f"Orchestra={urls.orchestra_url}, Adapters={urls.adapters_url}, "
        f"Comms={urls.comms_url}",
    )


def _local_stack_auto_manage_enabled() -> bool:
    return os.getenv("LOCAL_STACK_NO_AUTO", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }


def _stack_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("ORCHESTRA_REPO_PATH", str(_resolve_sibling_repo("orchestra")))
    env.setdefault(
        "COMMUNICATION_REPO_PATH",
        str(_resolve_sibling_repo("communication")),
    )
    env.setdefault("UNITY_REPO_PATH", str(_resolve_sibling_repo("unify")))
    env.setdefault("CONSOLE_REPO_PATH", str(_resolve_sibling_repo("console")))
    env.setdefault("UNIFY_STACK_ROOT", str(_resolve_unify_root()))
    # Keep Orchestra alive for the full pytest session; local.sh defaults to 600s.
    env.setdefault("ORCHESTRA_INACTIVITY_TIMEOUT_SECONDS", "0")
    return env


@dataclass
class ManagedLocalStack:
    """Tracks whether this pytest session started the local self-host stack."""

    started_by_session: bool
    urls: LocalStackUrls


def _seed_local_orchestra_rbac() -> None:
    """Seed system permissions and roles required for org creation on a fresh DB."""

    orchestra_repo = _resolve_sibling_repo("orchestra")
    seed_path = orchestra_repo / "orchestra" / "tests" / "seeding.sql"
    if not seed_path.is_file():
        raise FileNotFoundError(f"Orchestra RBAC seed file not found at {seed_path}")

    content = seed_path.read_text(encoding="utf-8")
    marker = "-- RBAC: Permissions"
    if marker not in content:
        raise RuntimeError(f"RBAC seed marker not found in {seed_path}")
    rbac_sql = content[content.index(marker) :]

    db_container = os.getenv("ORCHESTRA_DB_CONTAINER", "orchestra-local-db")
    print(f"Seeding local Orchestra RBAC via {seed_path.name}...")
    completed = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            db_container,
            "psql",
            "-U",
            "orchestra",
            "-d",
            "orchestra",
        ],
        input=rbac_sql,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Failed to seed local Orchestra RBAC: "
            f"{completed.stderr or completed.stdout}",
        )


def _purge_local_orchestra() -> None:
    """Destroy the local Orchestra container and database volume."""

    orchestra_repo = _resolve_sibling_repo("orchestra")
    local_script = orchestra_repo / "scripts" / "local.sh"
    if not local_script.is_file():
        raise FileNotFoundError(f"Orchestra local.sh not found at {local_script}")

    print(f"Purging local Orchestra via {local_script} purge...")
    subprocess.run(
        ["bash", str(local_script), "purge"],
        cwd=orchestra_repo,
        env=_stack_subprocess_env(),
        check=True,
        timeout=120,
    )


def _bootstrap_self_host(urls: LocalStackUrls) -> None:
    """Seed self-host platform defaults on a fresh local Orchestra DB."""

    orchestra_repo = _resolve_sibling_repo("orchestra")
    bootstrap_script = orchestra_repo / "scripts" / "bootstrap_self_host.sh"
    if not bootstrap_script.is_file():
        raise FileNotFoundError(
            f"Self-host bootstrap script not found at {bootstrap_script}",
        )

    env = _stack_subprocess_env()
    env["SELF_HOST"] = "1"
    env["PUBSUB_EMULATOR_HOST"] = urls.pubsub_emulator_host
    env.setdefault("GCP_PROJECT_ID", urls.gcp_project_id)
    print(f"Bootstrapping self-host platform defaults via {bootstrap_script}...")
    subprocess.run(
        ["bash", str(bootstrap_script)],
        cwd=orchestra_repo,
        env=env,
        check=True,
        timeout=120,
    )


def _reset_and_start_local_stack(urls: LocalStackUrls) -> None:
    """Tear down, purge Orchestra, and bring up a fresh local self-host stack."""

    unity_repo, stack_script = _resolve_stack_script()
    print(f"Stopping local stack via {stack_script} down...")
    subprocess.run(
        ["bash", str(stack_script), "down"],
        cwd=unity_repo,
        env=_stack_subprocess_env(),
        check=False,
        timeout=120,
    )
    _purge_local_orchestra()
    print(
        "Starting local self-host stack via "
        f"{stack_script} (this can take several minutes)...",
    )
    completed = subprocess.run(
        ["bash", str(stack_script), "up"],
        cwd=unity_repo,
        env=_stack_subprocess_env(),
        check=False,
        timeout=LOCAL_STACK_START_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        print(
            f"stack.sh up exited {completed.returncode}. "
            "Unity/Console startup may have failed; continuing if Orchestra, "
            "Adapters, and Comms are reachable.",
        )
    _seed_local_orchestra_rbac()
    _bootstrap_self_host(urls)


def _stop_local_stack() -> None:
    unity_repo, stack_script = _resolve_stack_script()
    print("Stopping local self-host stack...")
    subprocess.run(
        ["bash", str(stack_script), "down"],
        cwd=unity_repo,
        env=_stack_subprocess_env(),
        check=False,
        timeout=120,
    )


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
        "local_stack: tests that require the local self-host stack (stack.sh up)",
    )
    config.addinivalue_line(
        "markers",
        "invariant: which INV-N invariant(s) the test covers",
    )
    config.addinivalue_line(
        "markers",
        "slow: long-running tests (VM provision, etc.) — deselect with -m 'not slow'",
    )
    config.addinivalue_line(
        "markers",
        "merge_gate: minimal representative subset run live against staging to gate "
        "staging -> main merges",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


def pytest_sessionfinish(session, exitstatus):
    """Reap any test assistants whose own per-test teardown was skipped.

    Per-test ``finally`` blocks already delete the assistants they create; this
    is a best-effort backstop for assistants left behind when a test process is
    interrupted between creation and its own cleanup. It never raises so it
    cannot mask the real session outcome.
    """

    leftover = sorted(_created_test_assistant_ids)
    if not leftover:
        return
    for agent_id in leftover:
        try:
            _delete_test_assistant(agent_id, runtime_timeout=30)
        except Exception as exc:  # pragma: no cover - defensive backstop
            print(f"[Teardown] Session-end reap failed for {agent_id}: {exc}")


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
def _resolved_pubsub_credentials():
    """Resolve one explicit Pub/Sub identity for the integration-test session."""

    resolved = resolve_pubsub_credentials()
    print(f"[Pub/Sub] Using {resolved.describe()}")
    return resolved


@pytest.fixture
def pubsub_credentials(request, _resolved_pubsub_credentials):
    """Attach Pub/Sub credential context to failure artifacts for this test."""

    add_failure_context(
        request,
        "pubsub_credentials",
        _resolved_pubsub_credentials.as_context(),
    )
    return _resolved_pubsub_credentials


@pytest.fixture
def pubsub_publisher(pubsub_credentials):
    """Build a publisher client using the explicit integration credential path."""

    client = build_pubsub_publisher_client(pubsub_credentials)
    try:
        yield client
    finally:
        client.transport.close()


@pytest.fixture
def pubsub_subscriber(pubsub_credentials):
    """Build a subscriber client using the explicit integration credential path."""

    client = build_pubsub_subscriber_client(pubsub_credentials)
    try:
        yield client
    finally:
        client.transport.close()


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


def _recent_unity_container_logs(
    pod_names: list[str],
    job_names: list[str] | None = None,
    contains: str | None = None,
) -> list[str]:
    # Scope the query to the target pods server-side. A namespace-wide
    # newest-N sample gets flooded once many assistant pods stream verbose
    # actor logs concurrently, silently dropping the lines being polled for.
    # ``contains`` additionally pushes the substring match into the query so
    # a single sought line cannot be displaced by a burst of newer entries.
    terms = [str(name) for name in pod_names if name]
    if job_names:
        terms.extend(str(name) for name in job_names if name)
    terms = sorted(set(terms))
    if not terms:
        return []
    pod_clause = " OR ".join(f'resource.labels.pod_name:"{term}"' for term in terms)
    query = (
        'resource.type="k8s_container" AND '
        f'resource.labels.namespace_name="{NAMESPACE}" AND '
        f"({pod_clause})"
    )
    if contains:
        escaped = contains.replace("\\", "\\\\").replace('"', '\\"')
        query += f' AND textPayload:"{escaped}"'
    since = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query += f' AND timestamp >= "{since}"'
    try:
        output = subprocess.check_output(
            [
                "gcloud",
                "logging",
                "read",
                query,
                "--project=gcp-project-runtime",
                "--limit=500",
                "--format=value(timestamp,resource.labels.pod_name,textPayload)",
            ],
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return [f"unity log collection failed: {exc}"]

    lines = []
    for line in output.splitlines():
        if len(line) > 2000:
            line = line[:2000] + "... [truncated]"
        lines.append(line)
    return lines[-400:]


def _recent_k8s_pod_events(
    pod_names: list[str],
    job_names: list[str] | None = None,
) -> list[str]:
    if not pod_names and not job_names:
        return []
    since = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        output = subprocess.check_output(
            [
                "gcloud",
                "logging",
                "read",
                (
                    'resource.type="k8s_pod" AND '
                    f'resource.labels.namespace_name="{NAMESPACE}" AND '
                    f'timestamp >= "{since}"'
                ),
                "--project=gcp-project-runtime",
                "--limit=200",
                "--format=value(timestamp,resource.labels.pod_name,jsonPayload.reason,jsonPayload.message)",
            ],
            text=True,
            timeout=30,
        )
    except Exception as exc:
        return [f"pod event collection failed: {exc}"]

    terms = [str(name) for name in pod_names if name]
    if job_names:
        terms.extend(str(name) for name in job_names if name)
    lines = []
    for line in output.splitlines():
        if not any(term in line for term in terms):
            continue
        if len(line) > 2000:
            line = line[:2000] + "... [truncated]"
        lines.append(line)
    return lines[-80:]


@dataclass
class RuntimeIdentityTracker:
    """Tracks concrete runtime identities for richer failure artifacts.

    The live runtime may be torn down by a test's finally block before the
    artifact is assembled. Persisting the observed session/job/pod names lets
    us fetch Unity logs and Pod events by historical job prefix even after the
    current runtime snapshot is empty.
    """

    assistant_ids: set[str] = field(default_factory=set)
    session_names: set[str] = field(default_factory=set)
    job_names: set[str] = field(default_factory=set)
    pod_names: set[str] = field(default_factory=set)
    activation_ids: set[str] = field(default_factory=set)

    def track(
        self,
        *,
        assistant_id: str | None = None,
        session_name: str | None = None,
        job_name: str | None = None,
        pod_name: str | None = None,
        activation_id: str | None = None,
    ) -> None:
        if assistant_id:
            self.assistant_ids.add(str(assistant_id))
        if session_name:
            self.session_names.add(str(session_name))
        if job_name:
            self.job_names.add(str(job_name))
        if pod_name:
            self.pod_names.add(str(pod_name))
        if activation_id:
            self.activation_ids.add(str(activation_id))


def _track_runtime_identity(
    *,
    assistant_id: str | None = None,
    session_name: str | None = None,
    job_name: str | None = None,
    pod_name: str | None = None,
    activation_id: str | None = None,
) -> None:
    tracker = _CURRENT_RUNTIME_IDENTITY_TRACKER
    if tracker is None:
        return
    tracker.track(
        assistant_id=assistant_id,
        session_name=session_name,
        job_name=job_name,
        pod_name=pod_name,
        activation_id=activation_id,
    )


def add_failure_context(request, key: str, value: Any) -> None:
    """Attach structured test-specific evidence to the failure artifact."""
    current = getattr(request.node, "_extra_failure_context", None)
    if current is None:
        current = {}
        request.node._extra_failure_context = current
    current[key] = value


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
    tracker = getattr(request.node, "_runtime_identity_tracker", None)
    assistant_ids = sorted(
        set(_assistant_ids_from_request(request, batch_api))
        | set(_assistant_ids_from_violation_messages(new_violations))
        | (set(getattr(tracker, "assistant_ids", set())) if tracker else set()),
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
    tracked_session_names = (
        sorted(getattr(tracker, "session_names", set())) if tracker else []
    )
    tracked_job_names = sorted(getattr(tracker, "job_names", set())) if tracker else []
    tracked_pod_names = sorted(getattr(tracker, "pod_names", set())) if tracker else []
    extra_failure_context = getattr(request.node, "_extra_failure_context", None)
    pod_names = [
        pod.get("pod_name")
        for state in runtime.values()
        for pod in state.get("pods", [])
        if pod.get("pod_name")
    ]
    session_names = sorted(set(session_names) | set(tracked_session_names))
    pod_names = sorted(set(pod_names) | set(tracked_pod_names))
    bundle = {
        "test_nodeid": request.node.nodeid,
        "timestamp": datetime.now(UTC).isoformat(),
        "assistant_ids": assistant_ids,
        "tracked_runtime_identities": {
            "assistant_ids": (
                sorted(getattr(tracker, "assistant_ids", set())) if tracker else []
            ),
            "session_names": tracked_session_names,
            "job_names": tracked_job_names,
            "pod_names": tracked_pod_names,
            "activation_ids": (
                sorted(getattr(tracker, "activation_ids", set())) if tracker else []
            ),
        },
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
            "unity": _recent_unity_container_logs(
                pod_names,
                tracked_job_names,
            ),
            "pod_events": _recent_k8s_pod_events(
                pod_names,
                tracked_job_names,
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
    if extra_failure_context:
        bundle["extra_failure_context"] = extra_failure_context
    return _write_failure_artifact(bundle)


@pytest.fixture(autouse=True)
def runtime_identity_tracker(request):
    global _CURRENT_RUNTIME_IDENTITY_TRACKER
    previous = _CURRENT_RUNTIME_IDENTITY_TRACKER
    tracker = RuntimeIdentityTracker()
    request.node._runtime_identity_tracker = tracker
    request.node._extra_failure_context = {}
    _CURRENT_RUNTIME_IDENTITY_TRACKER = tracker
    try:
        yield tracker
    finally:
        _CURRENT_RUNTIME_IDENTITY_TRACKER = previous


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


@pytest.fixture(scope="session")
def local_stack_urls() -> LocalStackUrls:
    """Resolve local stack URLs from env vars and communication-local.config."""

    config_values = _parse_communication_local_config(LOCAL_COMMUNICATION_CONFIG)
    orchestra_url = os.getenv("TEST_ORCHESTRA_URL", DEFAULT_LOCAL_ORCHESTRA_URL).rstrip(
        "/",
    )
    adapters_url = os.getenv(
        "TEST_ADAPTERS_URL",
        config_values.get("UNITY_ADAPTERS_URL", DEFAULT_LOCAL_ADAPTERS_URL),
    ).rstrip("/")
    comms_url = os.getenv(
        "TEST_COMMS_APP_URL",
        config_values.get("UNITY_COMMS_URL", DEFAULT_LOCAL_COMMS_URL),
    ).rstrip("/")
    pubsub_emulator_host = os.getenv(
        "PUBSUB_EMULATOR_HOST",
        config_values.get("PUBSUB_EMULATOR_HOST", DEFAULT_LOCAL_PUBSUB_HOST),
    )
    gcp_project_id = os.getenv(
        "TEST_GCP_PROJECT_ID",
        config_values.get("GCP_PROJECT_ID", DEFAULT_LOCAL_GCP_PROJECT_ID),
    )
    pubsub_suffix = os.getenv(
        "TEST_PUBSUB_SUFFIX",
        "-staging" if NAMESPACE != "production" else "",
    )
    return LocalStackUrls(
        orchestra_url=orchestra_url,
        adapters_url=adapters_url,
        comms_url=comms_url,
        pubsub_emulator_host=pubsub_emulator_host,
        gcp_project_id=gcp_project_id,
        pubsub_suffix=pubsub_suffix,
    )


@pytest.fixture(scope="session")
def managed_local_stack(local_stack_urls) -> ManagedLocalStack:
    """Ensure a fresh local self-host stack for ``local_stack`` tests.

    When ``LOCAL_STACK_NO_AUTO`` is unset, runs ``stack down``, ``orchestra
    local.sh purge``, ``stack up``, and platform bootstrap. Credential-dependent
    tests require an explicit ``UNIFY_KEY`` because the self-host owner is now
    created only through the UI. Runs ``stack down`` on session teardown unless
    ``LOCAL_STACK_LEAVE_RUNNING=1``.
    """

    if not _local_stack_auto_manage_enabled():
        yield ManagedLocalStack(started_by_session=False, urls=local_stack_urls)
        return

    started_by_session = False
    try:
        _reset_and_start_local_stack(local_stack_urls)
        started_by_session = True
        bootstrap = _load_self_host_bootstrap_credentials()
        unify_key = os.getenv("UNIFY_KEY", bootstrap.get("api_key", "")).strip()
        admin_key = os.getenv("ORCHESTRA_ADMIN_KEY", DEFAULT_LOCAL_ADMIN_KEY).strip()
        if not unify_key:
            pytest.skip(
                "UNIFY_KEY required for credentialed local stack integration tests; "
                "register in Console or export a UI-created owner's key",
            )
        _apply_local_stack_credentials(unify_key=unify_key, admin_key=admin_key)
        _wait_for_local_stack(
            local_stack_urls,
            unify_key=unify_key,
            admin_key=admin_key,
        )
        yield ManagedLocalStack(
            started_by_session=True,
            urls=local_stack_urls,
        )
    finally:
        if started_by_session and os.getenv(
            "LOCAL_STACK_LEAVE_RUNNING",
            "",
        ).strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            _stop_local_stack()


@pytest.fixture(scope="session")
def require_local_stack(managed_local_stack, local_stack_urls) -> LocalStackUrls:
    """Require a reachable local self-host stack and resolved credentials."""

    unify_key, admin_key = _resolve_local_stack_credentials()
    if not unify_key:
        pytest.skip(
            "UNIFY_KEY required for local stack integration tests "
            "(set explicitly or start stack to populate bootstrap credentials)",
        )
    if not admin_key:
        pytest.skip("ORCHESTRA_ADMIN_KEY required for local stack integration tests")
    _apply_local_stack_credentials(unify_key=unify_key, admin_key=admin_key)

    if not _local_stack_is_ready(
        local_stack_urls,
        unify_key=unify_key,
        admin_key=admin_key,
    ):
        if _local_stack_auto_manage_enabled():
            pytest.fail(
                "Local stack auto-start did not produce a reachable stack at "
                f"Orchestra={local_stack_urls.orchestra_url}, "
                f"Adapters={local_stack_urls.adapters_url}, "
                f"Comms={local_stack_urls.comms_url}.",
            )
        pytest.skip(
            "Local self-host stack is not reachable. Run with auto-manage enabled "
            "(default) or start the stack manually and set LOCAL_STACK_NO_AUTO=1.",
        )

    os.environ.setdefault("PUBSUB_EMULATOR_HOST", local_stack_urls.pubsub_emulator_host)
    return local_stack_urls


@pytest.fixture
def local_stack_adapters(require_local_stack, admin_headers) -> AdaptersClient:
    """Adapters client pointed at the local stack."""

    return AdaptersClient(
        base_url=require_local_stack.adapters_url,
        headers=admin_headers,
    )


@pytest.fixture
def local_stack_comms(require_local_stack, admin_headers) -> CommsClient:
    """Comms App client pointed at the local stack."""

    return CommsClient(base_url=require_local_stack.comms_url, headers=admin_headers)


@pytest.fixture
def local_stack_pubsub_subscriber(require_local_stack):
    """Pub/Sub subscriber client configured for the local emulator."""

    credentials = resolve_pubsub_credentials()
    client = build_pubsub_subscriber_client(credentials)
    try:
        yield client
    finally:
        client.transport.close()


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


def _read_assistant_session_http(assistant_id: str) -> dict | None:
    """Read AssistantSession state directly from the deployed Comms app."""
    resp = requests.get(
        f"{COMMS_APP_URL}/infra/session/{assistant_id}",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=15,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _read_runtime_status_http(
    assistant_id: str,
    *,
    binding_id: str | None = None,
) -> dict:
    """Read the deployed runtime-cleanup status for an assistant."""
    resp = requests.get(
        f"{COMMS_APP_URL}/infra/runtime/{assistant_id}",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        params={"binding_id": binding_id} if binding_id else None,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def wait_for_assistant_runtime_quiesced(
    assistant_id: str,
    *,
    batch_api=None,
    timeout: float = 240,
    interval: float = 5,
) -> dict:
    """Wait until an assistant no longer owns live jobs, VMs, or disk attachments."""

    session_name = f"assistant-session-{str(assistant_id).lower().replace('_', '-')}"

    def _settled():
        runtime_status = _read_runtime_status_http(str(assistant_id))
        if runtime_status.get("active_job_names"):
            return None
        if runtime_status.get("owned_vms"):
            return None
        if runtime_status.get("other_owned_vms"):
            return None
        if runtime_status.get("disk_vm_name") is not None:
            return None
        return runtime_status

    return poll_until(
        _settled,
        timeout=timeout,
        interval=interval,
        description=(
            f"Assistant runtime {assistant_id} to release jobs, VMs, and disk attachments"
        ),
        failure_snapshot=lambda: {
            "runtime_status": _read_runtime_status_http(str(assistant_id)),
            "session": _read_assistant_session_http(str(assistant_id)),
            "assistant_jobs": (
                []
                if batch_api is None
                else [
                    job.metadata.name
                    for job in list_jobs_with_assistant_id(batch_api, str(assistant_id))
                ]
            ),
            "session_jobs": (
                []
                if batch_api is None
                else [
                    job.metadata.name
                    for job in list_jobs_with_session_ref(batch_api, session_name)
                ]
            ),
            "assigned_vms": assigned_vm_runtime_refs(str(assistant_id)),
        },
    )


def wait_for_assistant_runtime_stopped(
    assistant_id: str,
    *,
    batch_api=None,
    binding_id: str | None = None,
    timeout: float = 240,
    interval: float = 5,
) -> dict | None:
    """Wait until runtime cleanup completes for an assistant or binding."""

    session_name = f"assistant-session-{str(assistant_id).lower().replace('_', '-')}"

    def _settled():
        runtime_status = _read_runtime_status_http(
            str(assistant_id),
            binding_id=binding_id,
        )
        if binding_id:
            if not runtime_status.get("binding_runtime_cleanup_complete"):
                return None
            return runtime_status
        if not runtime_status.get("runtime_cleanup_complete"):
            return None
        if runtime_status.get("assistant_session_exists"):
            if runtime_status.get("assistant_session_phase") != "Released":
                return None
            if runtime_status.get("assistant_session_desired_state") != "Stopped":
                return None
        return runtime_status

    return poll_until(
        _settled,
        timeout=timeout,
        interval=interval,
        description=(
            f"Binding runtime {binding_id} for assistant {assistant_id} to finish cleanup"
            if binding_id
            else (
                f"Assistant runtime {assistant_id} to reach Released with no bound "
                "resources"
            )
        ),
        failure_snapshot=lambda: {
            "runtime_status": _read_runtime_status_http(
                str(assistant_id),
                binding_id=binding_id,
            ),
            "binding_id": binding_id,
            "session": _read_assistant_session_http(str(assistant_id)),
            "assistant_jobs": (
                []
                if batch_api is None
                else [
                    job.metadata.name
                    for job in list_jobs_with_assistant_id(batch_api, str(assistant_id))
                ]
            ),
            "binding_jobs": (
                []
                if batch_api is None or not binding_id
                else [
                    job.metadata.name
                    for job in list_jobs_with_binding_id(batch_api, binding_id)
                ]
            ),
            "session_jobs": (
                []
                if batch_api is None
                else [
                    job.metadata.name
                    for job in list_jobs_with_session_ref(batch_api, session_name)
                ]
            ),
            "assigned_vms": assigned_vm_runtime_refs(str(assistant_id)),
        },
    )


def stop_assistant_runtime(
    assistant_id: str,
    *,
    batch_api=None,
    timeout: float = 240,
    strict: bool = False,
    context: str | None = None,
) -> None:
    """Stop an assistant runtime through AssistantSession desired state.

    Args:
        assistant_id: Assistant whose runtime should be stopped.
        batch_api: Optional BatchV1Api for richer timeout snapshots.
        timeout: How long to wait for ``/infra/runtime`` cleanup convergence.
        strict: When True, request or convergence failures raise immediately
            instead of being logged and swallowed.
        context: Short label describing the cleanup phase for diagnostics.
    """
    prefix = "[Cleanup]" if not context else f"[Cleanup:{context}]"

    try:
        resp = requests.post(
            f"{COMMS_APP_URL}/infra/session/{assistant_id}/stop",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=90,
        )
    except Exception as exc:
        message = f"{prefix} Failed to request session stop for {assistant_id}: {exc}"
        if strict:
            raise AssertionError(message) from exc
        print(message)
        return

    if resp.status_code not in (200, 404):
        message = (
            f"{prefix} Session stop for {assistant_id} returned "
            f"{resp.status_code}: {resp.text[:200]}"
        )
        if strict:
            raise AssertionError(message)
        print(message)
        return

    stop_result = {}
    try:
        stop_result = resp.json()
    except Exception:
        stop_result = {}
    target_binding_id = str(stop_result.get("binding_id") or "") or None

    try:
        wait_for_assistant_runtime_stopped(
            str(assistant_id),
            batch_api=batch_api,
            binding_id=target_binding_id,
            timeout=timeout,
        )
    except Exception as exc:
        if strict:
            raise
        print(f"{prefix} Runtime stop wait failed for {assistant_id}: {exc}")


def _delete_assistant_session_if_present(
    assistant_id: str,
    *,
    timeout: float,
    interval: float = 5,
) -> bool:
    """Delete a lingering AssistantSession object if it still exists."""

    session = _read_assistant_session_http(str(assistant_id))
    if session is None:
        return False

    resp = requests.delete(
        f"{COMMS_APP_URL}/infra/session/{assistant_id}",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=90,
    )
    if resp.status_code not in (200, 404):
        raise AssertionError(
            f"DELETE /infra/session/{assistant_id} returned "
            f"{resp.status_code}: {resp.text[:200]}",
        )
    if resp.status_code == 404:
        return False

    poll_until(
        lambda: _read_assistant_session_http(str(assistant_id)) is None,
        timeout=timeout,
        interval=interval,
        description=f"AssistantSession for {assistant_id} to be deleted",
        failure_snapshot=lambda: _read_assistant_session_http(str(assistant_id)),
    )
    return True


def _delete_assistant_disk_if_present(assistant_id: str) -> bool:
    """Delete a lingering assistant disk if it still exists."""

    resp = requests.delete(
        f"{COMMS_APP_URL}/infra/vm/pool/disk/{assistant_id}",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    if resp.status_code == 404:
        return False
    if resp.status_code != 200:
        raise AssertionError(
            f"DELETE /infra/vm/pool/disk/{assistant_id} returned "
            f"{resp.status_code}: {resp.text[:200]}",
        )
    return True


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


def wait_for_idle_vm_pool(
    gce_client,
    min_idle: int = 1,
    *,
    vm_type: str = "ubuntu",
    timeout: float = 180,
):
    """Wait until the VM pool has at least min_idle idle VMs."""
    poll_until(
        lambda: len(list_idle_vms(gce_client, vm_type=vm_type)) >= min_idle,
        timeout=timeout,
        interval=10,
        description=f"Idle VM pool to have >= {min_idle} {vm_type} VMs",
        failure_snapshot=lambda: {
            "idle_vm_names": [
                vm.name for vm in list_idle_vms(gce_client, vm_type=vm_type)
            ],
            "stopped_vm_names": [
                vm.name for vm in list_stopped_vms(gce_client, vm_type=vm_type)
            ],
        },
    )


def purge_quarantined_pool_vms(comms_client, vm_type: str = "ubuntu") -> dict:
    """Delete quarantined pool VMs so replenish can create fresh replacements."""
    resp = comms_client.post(
        "/infra/vm/pool/purge-quarantined",
        params={"vm_type": vm_type},
        timeout=120,
    )
    assert (
        resp.status_code == 200
    ), f"purge-quarantined failed: {resp.status_code} {resp.text}"
    return resp.json()


def assign_pool_vm_with_cold_start(
    comms_client,
    payload: dict,
    *,
    poll=poll_until,
    timeout: float = 600,
    interval: float = 15,
):
    """Assign a pool VM, tolerating cold-start when the warm idle pool is empty.

    With ``POOL_TARGET_IDLE=0``, ``/infra/vm/pool/assign`` returns 503 and kicks
    replenish when no idle VM is claimable. Callers that need a VM immediately
    must poll through that deferred-retry window (and clear quarantined VMs that
    would otherwise block slot reuse).
    """
    vm_type = str(payload.get("vm_type") or "ubuntu")
    purge_quarantined_pool_vms(comms_client, vm_type=vm_type)

    last_resp = None

    def _attempt():
        nonlocal last_resp
        last_resp = comms_client.post(
            "/infra/vm/pool/assign",
            json=payload,
            timeout=120,
        )
        if last_resp.status_code == 200:
            return last_resp
        if last_resp.status_code == 503 and "No idle" in last_resp.text:
            # Assign already triggers demand-aware replenish; keep polling until
            # the newly provisioned VM becomes claimable.
            return None
        raise AssertionError(
            f"VM assign failed: {last_resp.status_code} {last_resp.text}",
        )

    return poll(
        _attempt,
        timeout=timeout,
        interval=interval,
        description=(
            f"cold-start pool assign for assistant "
            f"{payload.get('assistant_id')} ({vm_type})"
        ),
        failure_snapshot=lambda: {
            "last_status": getattr(last_resp, "status_code", None),
            "last_body": getattr(last_resp, "text", None),
            "payload": payload,
        },
    )


@dataclass
class JobTracker:
    """Tracks created Jobs and cleans them up via the correct authority."""

    jobs: list = field(default_factory=list)
    batch_api: Any = None
    namespace: str = NAMESPACE

    def track(self, job_name: str):
        self.jobs.append(job_name)

    def cleanup(self):
        if not self.batch_api:
            return
        assistant_ids: set[str] = set()
        for name in self.jobs:
            try:
                job = self.batch_api.read_namespaced_job(
                    name=name,
                    namespace=self.namespace,
                )
            except Exception:
                continue
            labels = dict(job.metadata.labels or {})
            annotations = dict(job.metadata.annotations or {})
            assistant_id = str(labels.get("assistant-id", "") or "").strip()
            session_backed = bool(
                labels.get(ASSISTANT_SESSION_REF_LABEL)
                or annotations.get(ASSISTANT_SESSION_REF_ANNOTATION),
            )
            if assistant_id and session_backed:
                assistant_ids.add(assistant_id)
                continue
            try:
                self.batch_api.delete_namespaced_job(
                    name=name,
                    namespace=self.namespace,
                    propagation_policy="Foreground",
                )
            except Exception:
                pass
        for assistant_id in sorted(assistant_ids):
            stop_assistant_runtime(
                assistant_id,
                batch_api=self.batch_api,
                timeout=180,
            )
        if self.jobs:
            replenish_pool()


@pytest.fixture
def job_tracker(batch_api):
    tracker = JobTracker(batch_api=batch_api)
    yield tracker
    tracker.cleanup()


@pytest.fixture(autouse=True)
def ensure_pool_capacity(request):
    """Before each test, ensure the idle pool has at least 1 container.

    If a previous test consumed containers and replenishment hasn't finished,
    wait up to 90s for the pool to refill. This prevents cascading failures
    from pool exhaustion.
    """
    if request.node.get_closest_marker("local_stack"):
        yield
        return

    batch_api = request.getfixturevalue("k8s_clients")[0]
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


def list_jobs_with_binding_id(
    batch_api,
    binding_id: str,
    namespace: str = NAMESPACE,
) -> list:
    """List all active Jobs with a specific binding-id label."""

    sanitized = binding_id.lower().replace("_", "-")
    jobs = batch_api.list_namespaced_job(
        namespace=namespace,
        label_selector=f"app=unity,{ASSISTANT_SESSION_BINDING_LABEL}={sanitized}",
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


def cleanup_assistant_jobs(
    batch_api,
    assistant_ids: list[str],
    *,
    strict: bool = False,
    context: str | None = None,
    timeout: float = 180,
    parallelism: int = 1,
):
    """Stop assistant runtimes through AssistantSession and expire test records.

    Args:
        batch_api: BatchV1Api used for runtime-status snapshots.
        assistant_ids: Assistants whose runtimes should be stopped.
        strict: When True, stop failures raise immediately.
        context: Short label describing the caller's cleanup phase.
        timeout: Per-assistant runtime cleanup wait timeout in seconds.
        parallelism: Maximum number of assistants to clean up concurrently.
    """
    unique_ids = [str(aid) for aid in dict.fromkeys(str(aid) for aid in assistant_ids)]

    def _cleanup_one(aid: str) -> None:
        stop_assistant_runtime(
            aid,
            batch_api=batch_api,
            timeout=timeout,
            strict=strict,
            context=context,
        )
        try:
            expire_test_assistant_records(aid)
        except Exception:
            pass

    if parallelism <= 1 or len(unique_ids) <= 1:
        for aid in unique_ids:
            _cleanup_one(aid)
        return

    max_workers = max(1, min(len(unique_ids), parallelism))
    failures: list[tuple[str, Exception]] = []
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="assistant-runtime-cleanup",
    ) as pool:
        future_to_id = {pool.submit(_cleanup_one, aid): aid for aid in unique_ids}
        for future in as_completed(future_to_id):
            aid = future_to_id[future]
            try:
                future.result()
            except Exception as exc:  # pragma: no cover - unexpected helper failure
                if strict:
                    failures.append((aid, exc))
                else:
                    prefix = "[Cleanup]" if not context else f"[Cleanup:{context}]"
                    print(f"{prefix} Unexpected cleanup error for {aid}: {exc}")

    if failures:
        aid, exc = failures[0]
        raise AssertionError(f"Cleanup failed for {aid}: {exc}") from exc


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
TEST_ASSISTANT_FACTORY_FIRST_NAME = "InfraTest"
TEST_ASSISTANT_FACTORY_ABOUT = (
    "Stress test assistant (auto-created by integration tests)"
)
TEST_ASSISTANT_DELETE_TIMEOUT_SECONDS = 120
TEST_ASSISTANT_CLEANUP_PARALLELISM = 6


def _factory_assistant_surname(index: int) -> str:
    """Return an alphabetic surname accepted by Unity contact validation."""

    value = abs(int(index))
    letters: list[str] = []
    while True:
        value, remainder = divmod(value, 26)
        letters.append(chr(ord("A") + remainder))
        if value == 0:
            break
    return f"Run{''.join(reversed(letters))}"


def _list_owned_assistants() -> list[dict]:
    """Return assistants visible to the current test user."""
    if not UNIFY_KEY:
        return []
    try:
        resp = requests.get(
            f"{ORCHESTRA_URL}/assistant",
            headers={"Authorization": f"Bearer {UNIFY_KEY}"},
            timeout=20,
        )
    except Exception:
        return []
    if resp.status_code != 200:
        return []
    data = resp.json()
    assistants = data.get("info", data) if isinstance(data, dict) else data
    return assistants if isinstance(assistants, list) else []


def _is_factory_test_assistant(assistant: dict) -> bool:
    """Return whether an Orchestra record belongs to the test factory."""
    if not isinstance(assistant, dict):
        return False
    if str(assistant.get("first_name") or "") != TEST_ASSISTANT_FACTORY_FIRST_NAME:
        return False
    is_local = assistant.get("is_local")
    if is_local not in (None, True):
        return False
    deploy_env = str(assistant.get("deploy_env") or "")
    if deploy_env and deploy_env != NAMESPACE:
        return False
    about = str(assistant.get("about") or "")
    surname = str(assistant.get("surname") or "")
    return about == TEST_ASSISTANT_FACTORY_ABOUT or (
        is_local is True and bool(surname.isdigit())
    )


def _stale_factory_test_assistant_ids(*, keep_ids: set[str] | None = None) -> list[str]:
    """Return prior-run factory assistants that should be deleted."""
    keep = {str(aid) for aid in (keep_ids or set())}
    stale_ids = []
    for assistant in _list_owned_assistants():
        agent_id = str(assistant.get("agent_id") or assistant.get("id") or "").strip()
        if not agent_id or agent_id in keep:
            continue
        if _is_factory_test_assistant(assistant):
            stale_ids.append(agent_id)
    return sorted(dict.fromkeys(stale_ids))


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
        "user_desktops": json.dumps(a.get("user_desktops", [])),
        "is_coordinator": str(a.get("is_coordinator", False)).lower(),
        "team_ids": json.dumps(a.get("team_ids", [])),
        "team_summaries": json.dumps(a.get("team_summaries", [])),
        "self_contact_id": str(a["self_contact_id"]),
        "boss_contact_id": str(a["boss_contact_id"]),
        "org_id": (
            str(a.get("organization_id", "")) if a.get("organization_id") else ""
        ),
    }


# ---------------------------------------------------------------------------
# Created-assistant tracking
#
# Every test assistant created through _create_test_assistant is recorded so a
# session-end backstop (and, in CI, an out-of-process cleanup step) can delete
# any assistant whose own per-test teardown was skipped — e.g. when the runner
# is hard-killed mid-test. Leaked assistants keep a Pub/Sub topic (and possibly
# a disk/VM) alive, so reaping them keeps recurring gate runs cost-neutral.
# ---------------------------------------------------------------------------

CI_CREATED_ASSISTANTS_FILE = os.getenv("CI_CREATED_ASSISTANTS_FILE", "")
_created_test_assistant_ids: set[str] = set()


def _register_created_test_assistant(agent_id: str) -> None:
    """Record a freshly created test assistant for end-of-session reaping."""

    agent_id = str(agent_id).strip()
    if not agent_id:
        return
    _created_test_assistant_ids.add(agent_id)
    if CI_CREATED_ASSISTANTS_FILE:
        try:
            with open(CI_CREATED_ASSISTANTS_FILE, "a", encoding="utf-8") as handle:
                handle.write(f"{agent_id}\n")
        except OSError as exc:
            print(f"[Teardown] Could not record created assistant {agent_id}: {exc}")


def _create_test_assistant(
    index: int,
    *,
    desktop_mode: str | None = "ubuntu",
    is_local: bool = True,
) -> dict:
    """Create a test assistant on Orchestra and return its full data.

    Calls POST /v0/assistant with create_infra=True (provisions Pub/Sub
    topic). By default the helper keeps assistants local so tests can opt
    into remote runtime wakeups only when they need them. Then fetches the
    full admin record to get api_key and user fields.
    """
    assert UNIFY_KEY, "UNIFY_KEY required to create test assistants"
    assert ADMIN_KEY, "ORCHESTRA_ADMIN_KEY required to fetch admin records"

    payload = {
        "first_name": TEST_ASSISTANT_FACTORY_FIRST_NAME,
        "surname": _factory_assistant_surname(index),
        "age": 25,
        "nationality": "North America",
        "about": TEST_ASSISTANT_FACTORY_ABOUT,
        "is_local": is_local,
        "create_infra": True,
        "timezone": "UTC",
    }
    if desktop_mode is not None:
        payload["desktop_mode"] = desktop_mode
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

    _register_created_test_assistant(agent_id)

    admin_info = admin_resp.json()["info"]
    a = admin_info[0] if isinstance(admin_info, list) else admin_info
    return _admin_record_to_data(a)


def _delete_test_assistant(
    agent_id: str,
    batch_api=None,
    runtime_timeout: float = TEST_ASSISTANT_DELETE_TIMEOUT_SECONDS,
):
    """Delete a test assistant from Orchestra and clean up infra resources.

    Calls DELETE /v0/assistant/{id} which handles Pub/Sub, disks, phones,
    emails, and DB cleanup. Tests then defensively expire AssistantJobs
    records, wait for the Comms runtime to quiesce, and prune any lingering
    AssistantSession or assistant-disk artifacts so repeated test runs stay
    idempotent even when upstream cleanup lags.

    Swallows all exceptions so teardown never aborts mid-way.
    """
    int_id = str(agent_id).split(".")[0]

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

    try:
        wait_for_assistant_runtime_quiesced(
            str(agent_id),
            batch_api=batch_api,
            timeout=runtime_timeout,
            interval=5,
        )
    except Exception as exc:
        print(f"[Teardown] Runtime quiesce wait failed for {agent_id}: {exc}")

    try:
        if _delete_assistant_session_if_present(
            str(agent_id),
            timeout=runtime_timeout,
            interval=5,
        ):
            print(f"[Teardown] Deleted AssistantSession for {agent_id}")
    except Exception as exc:
        print(f"[Teardown] Session delete cleanup failed for {agent_id}: {exc}")

    try:
        if _delete_assistant_disk_if_present(str(agent_id)):
            print(f"[Teardown] Deleted assistant disk for {agent_id}")
    except Exception as exc:
        print(f"[Teardown] Disk delete cleanup failed for {agent_id}: {exc}")


def _delete_test_assistants(
    assistant_ids: list[str],
    *,
    batch_api=None,
    runtime_timeout: float = TEST_ASSISTANT_DELETE_TIMEOUT_SECONDS,
    parallelism: int = TEST_ASSISTANT_CLEANUP_PARALLELISM,
) -> None:
    """Delete multiple test assistants concurrently."""
    unique_ids = [str(aid) for aid in dict.fromkeys(str(aid) for aid in assistant_ids)]
    if not unique_ids:
        return

    max_workers = max(1, min(len(unique_ids), parallelism))
    if max_workers == 1:
        for agent_id in unique_ids:
            _delete_test_assistant(
                agent_id,
                batch_api=batch_api,
                runtime_timeout=runtime_timeout,
            )
        return

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="test-assistant-delete",
    ) as pool:
        futures = [
            pool.submit(
                _delete_test_assistant,
                agent_id,
                batch_api,
                runtime_timeout,
            )
            for agent_id in unique_ids
        ]
        for future in as_completed(futures):
            future.result()


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

    batch_api = k8s_clients[0]
    stale_assistant_ids = _stale_factory_test_assistant_ids()
    if stale_assistant_ids:
        print(
            f"\n[Setup] Deleting {len(stale_assistant_ids)} stale test assistant(s) "
            "from prior runs...",
        )
        _delete_test_assistants(
            stale_assistant_ids,
            batch_api=batch_api,
            runtime_timeout=TEST_ASSISTANT_DELETE_TIMEOUT_SECONDS,
        )

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

    print(f"\n[Teardown] Deleting {len(created)} test assistants...")
    _delete_test_assistants(
        [a["assistant_id"] for a in created],
        batch_api=batch_api,
        runtime_timeout=TEST_ASSISTANT_DELETE_TIMEOUT_SECONDS,
    )
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
            "contact_id": int(assistant_data["boss_contact_id"]),
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
        vm = vms[0]
        labels = dict(vm.labels or {})
        aid = labels.get("assistant-id")
        binding_id = str(labels.get("binding-id", "") or "")
        hostname = _vm_metadata_value(vm, "hostname")
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
                "vm_name": vm.name,
                "binding_id": binding_id,
                "hostname": hostname,
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
    from google.api_core.exceptions import DeadlineExceeded, PermissionDenied

    sub_name = f"unity-{assistant_id}{_PUBSUB_SUFFIX}-outbound-sub"
    sub_path = subscriber.subscription_path(GCP_PROJECT_ID, sub_name)
    try:
        response = subscriber.pull(
            request={"subscription": sub_path, "max_messages": max_messages},
            timeout=timeout,
        )
    except DeadlineExceeded:
        return []
    except PermissionDenied as exc:
        credential_context = client_credential_context(subscriber)
        context_suffix = (
            f" Resolved Pub/Sub credentials: {json.dumps(credential_context, sort_keys=True)}"
            if credential_context
            else ""
        )
        raise AssertionError(
            "Pub/Sub subscriber lacks permission to consume "
            f"{sub_path}.{context_suffix}",
        ) from exc

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
    jobs = poll_until(
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
    for job in jobs:
        annotations = dict(job.metadata.annotations or {})
        _track_runtime_identity(
            assistant_id=str(assistant_id),
            session_name=(
                annotations.get(ASSISTANT_SESSION_REF_LABEL)
                or annotations.get(ASSISTANT_SESSION_REF_ANNOTATION)
            ),
            job_name=job.metadata.name,
        )
    return jobs


def _assistant_readiness_snapshot(
    assistant_id: str,
    *,
    batch_api,
    core_api=None,
    gce_client=None,
    unity_log_contains: str | None = None,
) -> dict[str, Any]:
    try:
        runtime = describe_runtime_state(
            batch_api,
            core_api,
            gce_client,
            str(assistant_id),
        )
    except Exception as exc:
        runtime = {"error": f"describe_runtime_state failed: {exc}"}
    session = runtime.get("session") or {}
    session_name = (session.get("metadata") or {}).get("name") or ""
    tracker = _CURRENT_RUNTIME_IDENTITY_TRACKER
    tracked_job_names = [
        name
        for name in sorted(getattr(tracker, "job_names", set()) if tracker else set())
        if name
    ]
    tracked_pod_names = [
        name
        for name in sorted(getattr(tracker, "pod_names", set()) if tracker else set())
        if name
    ]
    pod_names = [
        pod.get("pod_name") for pod in runtime.get("pods", []) if pod.get("pod_name")
    ]
    pod_names = sorted(set(pod_names) | set(tracked_pod_names))
    try:
        runtime_status = _read_runtime_status_http(str(assistant_id))
    except Exception as exc:
        runtime_status = {"error": f"runtime status read failed: {exc}"}
    return {
        "assistant_id": str(assistant_id),
        "runtime_status": runtime_status,
        "runtime": runtime,
        "recent_logs": {
            "controller": _recent_controller_logs(
                [str(assistant_id)],
                [session_name] if session_name else [],
            ),
            "comms": _recent_cloud_run_logs(
                COMMS_APP_URL,
                [str(assistant_id)],
                [session_name] if session_name else [],
            ),
            "adapters": _recent_cloud_run_logs(
                ADAPTERS_URL,
                [str(assistant_id)],
                [session_name] if session_name else [],
            ),
            "unity": _recent_unity_container_logs(
                pod_names,
                tracked_job_names,
                contains=unity_log_contains,
            ),
            "pod_events": _recent_k8s_pod_events(
                pod_names,
                tracked_job_names,
            ),
        },
    }


def wait_for_assistant_container_ready(
    assistant_id: str,
    *,
    batch_api,
    core_api=None,
    gce_client=None,
    timeout: float = 300,
    interval: float = 5,
) -> dict[str, Any]:
    """Wait until AssistantSession reports ContainerReady=True.

    This is a stronger readiness gate than ``job.status.active``. It proves
    Unity successfully discovered the AssistantSession binding, read the
    bootstrap Secret, published the StartupEvent, and patched the Job's
    container-ready annotation.
    """

    deadline = time.monotonic() + timeout
    last_phase = ""
    last_error = ""
    while time.monotonic() < deadline:
        session = _read_assistant_session_http(str(assistant_id))
        if session is not None:
            status = session.get("status") or {}
            binding = status.get("binding") or {}
            job_ref = binding.get("jobRef") or {}
            pod_ref = binding.get("podRef") or {}
            _track_runtime_identity(
                assistant_id=str(assistant_id),
                session_name=((session.get("metadata") or {}).get("name") or ""),
                activation_id=str(
                    (session.get("spec") or {}).get("activationId", "") or "",
                ),
                job_name=str(job_ref.get("name", "") or ""),
                pod_name=str(pod_ref.get("name", "") or ""),
            )
            last_phase = str(status.get("phase", "") or "")
            last_error = str(status.get("lastError", "") or "")
            conditions = status.get("conditions") or []
            container_ready = any(
                cond.get("type") == "ContainerReady" and cond.get("status") == "True"
                for cond in conditions
            )
            if container_ready:
                return session
            if last_phase == "Failed":
                snapshot = _assistant_readiness_snapshot(
                    str(assistant_id),
                    batch_api=batch_api,
                    core_api=core_api,
                    gce_client=gce_client,
                )
                raise AssertionError(
                    f"Assistant {assistant_id} reached Failed before ContainerReady. "
                    f"lastError={last_error or '(empty)'}\n"
                    f"Failure snapshot:\n{json.dumps(snapshot, indent=2, sort_keys=True, default=str)}",
                )
        time.sleep(interval)

    snapshot = _assistant_readiness_snapshot(
        str(assistant_id),
        batch_api=batch_api,
        core_api=core_api,
        gce_client=gce_client,
    )
    raise TimeoutError(
        f"Timed out after {timeout}s waiting for assistant {assistant_id} to reach "
        f"ContainerReady=True. Last phase={last_phase or '(none)'} "
        f"lastError={last_error or '(empty)'}.\n"
        f"Failure snapshot:\n{json.dumps(snapshot, indent=2, sort_keys=True, default=str)}",
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


def probe_vm_agent_service(hostname: str, timeout: float = 5.0) -> bool:
    """Check if the VM's agent-service is reachable through Caddy.

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


def probe_vm_agent_service_authenticated(
    hostname: str,
    api_key: str,
    command: str = "echo ok",
) -> requests.Response | None:
    """Call `/api/exec` with auth and return the VM's response, if any."""
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


def _vm_metadata_value(vm, key: str) -> str:
    """Return a metadata value from a GCE VM instance."""

    for item in getattr(getattr(vm, "metadata", None), "items", []) or []:
        if item.key == key:
            return str(item.value or "")
    return ""


def assigned_vm_runtime_refs(
    assistant_id: str,
    *,
    gce_client=None,
) -> list[dict[str, str]]:
    """Return binding-aware runtime refs for currently assigned VMs."""

    try:
        if gce_client is None:
            from google.cloud import compute_v1

            gce_client = compute_v1.InstancesClient()
        assigned_vms = list_assigned_vms(gce_client, assistant_id)
    except Exception:
        return []

    refs: list[dict[str, str]] = []
    for vm in assigned_vms:
        labels = dict(vm.labels or {})
        binding_id = str(labels.get("binding-id", "") or "")
        vm_name = str(getattr(vm, "name", "") or "")
        if not binding_id or not vm_name:
            continue
        refs.append(
            {
                "binding_id": binding_id,
                "vm_name": vm_name,
                "hostname": _vm_metadata_value(vm, "hostname"),
            },
        )
    return refs


def release_assigned_vms(
    assistant_id: str,
    *,
    gce_client=None,
    timeout: float = 30,
) -> list[requests.Response]:
    """Best-effort release of all VMs currently assigned to an assistant."""

    responses: list[requests.Response] = []
    for vm_ref in assigned_vm_runtime_refs(assistant_id, gce_client=gce_client):
        responses.append(
            requests.post(
                f"{COMMS_APP_URL}/infra/vm/pool/release",
                json={
                    "assistant_id": str(assistant_id),
                    "binding_id": vm_ref["binding_id"],
                    "vm_name": vm_ref["vm_name"],
                },
                headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                timeout=timeout,
            ),
        )
    return responses


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
    binding_jobs_map = {}
    idle_count = 0

    for job in jobs:
        labels = job.metadata.labels or {}
        status = labels.get("unity-status", "")
        aid = labels.get("assistant-id", "")
        session_name = labels.get(ASSISTANT_SESSION_REF_LABEL, "")
        binding_id = labels.get(ASSISTANT_SESSION_BINDING_LABEL, "")
        has_active = job.status.active and job.status.active > 0

        if job.metadata.deletion_timestamp and (aid or status not in ("", "idle")):
            violations.append(
                InvariantViolation(
                    "INV-8",
                    f"Deleting Job {job.metadata.name} still looks live "
                    f"(assistant-id={aid or 'empty'}, unity-status={status or 'empty'})",
                ),
            )

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

            if session_name and binding_id:
                binding_key = (session_name, binding_id)
                if binding_key in binding_jobs_map:
                    violations.append(
                        InvariantViolation(
                            "INV-1",
                            "Duplicate binding: "
                            f"{session_name}/{binding_id} has Jobs "
                            f"{binding_jobs_map[binding_key]} and {job.metadata.name}",
                        ),
                    )
                binding_jobs_map[binding_key] = job.metadata.name

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
def check_invariants_after_test(request):
    """After each test, check for NEW invariant violations.

    Reports violations as warnings rather than failing the test, because
    violations from previous tests (e.g., duplicate containers from the
    split-brain test) can cascade and cause false failures on unrelated tests.
    The invariant checker test (test_invariant_checker.py) is the authoritative
    place for invariant assertions.
    """
    if request.node.get_closest_marker("local_stack"):
        yield
        return

    invariant_baseline = request.getfixturevalue("invariant_baseline")
    k8s_clients = request.getfixturevalue("k8s_clients")
    gce_client = request.getfixturevalue("gce_client")
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
                f"bindingId={((status.get('binding') or {}).get('id'))} "
                f"jobRef={(((status.get('binding') or {}).get('jobRef') or {}).get('name'))} "
                f"vmRef={(((status.get('binding') or {}).get('vmRef') or {}).get('name'))} "
                f"lastError={status.get('lastError')}\n"
            )
        if artifact_path:
            msg += f"  [ARTIFACT] {artifact_path}\n"
        import warnings

        warnings.warn(msg)
