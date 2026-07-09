"""Integration tests for the full assistant deletion and session teardown flow.

Tests cover three scenarios and are designed to run unchanged against any
deployed environment (staging, main).  Session-CRD-specific
assertions are automatically skipped when the AssistantSession controller
is not deployed on the target cluster.

1. Fast delete response (test_delete_assistant_response_is_fast)
   DELETE /v0/assistant/{id} must return < DELETE_RESPONSE_TIMEOUT_SECONDS
   regardless of how much cleanup is queued.  Works in all environments.

2. Full background cleanup (test_delete_assistant_runtime_cleanup_completes)
   After deletion, the background task must free every runtime resource:
   K8s job, pool VM, Pub/Sub topic, AssistantCleanupTask status.
   If the AssistantSession CRD is present on the cluster, also verifies
   that the session object itself is cleaned up.

3. Session endpoint teardown (test_delete_session_endpoint_tears_down_pending_runtime)
   DELETE /infra/session/{id} must tear down the K8s job, pool VM, and
   bootstrap secret.  Requires the AssistantSession CRD; automatically
   skipped when the controller is not deployed on the target cluster.

Run against the default environment (staging):
    pytest tests/communication/infra/integration/test_assistant_session_teardown.py -v -s

Run against staging:
    TEST_NAMESPACE=staging \\
    pytest tests/communication/infra/integration/test_assistant_session_teardown.py -v -s
"""

import time

import pytest
import requests
from kubernetes.client.rest import ApiException

from communication.infra.assistant_sessions import (
    assistant_session_name,
)

from .conftest import (
    ADMIN_KEY,
    GCP_PROJECT_ID,
    NAMESPACE,
    ORCHESTRA_URL,
    UNIFY_KEY,
    _PUBSUB_SUFFIX,
    _create_test_assistant,
    _delete_test_assistant,
    _read_runtime_status_http,
    get_assistant_session,
    list_assigned_vms,
    list_jobs_with_assistant_id,
    purge_quarantined_pool_vms,
    start_real_job,
    wait_for_assistant_runtime_stopped,
)

pytestmark = [pytest.mark.integration]

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

# DELETE must respond within this many seconds (background task approach).
# The old blocking path regularly took 20-30 s; < 10 s confirms it works.
DELETE_RESPONSE_TIMEOUT_SECONDS = 10

# How long to wait for the background cleanup task to finish.
# Cold-start VM release + Orchestra Cloud Run CPU-throttling after the DELETE
# response can leave the durable task pending until an explicit redrive.
CLEANUP_TIMEOUT_SECONDS = 480

# CRD coordinates (must match common/settings.py)
_SESSION_CRD_GROUP = "infra.unify.ai"
_SESSION_CRD_VERSION = "v1alpha1"
_SESSION_CRD_PLURAL = "assistantsessions"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def has_session_crd(k8s_clients) -> bool:
    """True if the AssistantSession CRD is registered on the target cluster.

    Probes the K8s API directly so the result is accurate regardless of
    which NAMESPACE / environment the suite is running against.  Tests that
    require the CRD call ``pytest.skip`` when this returns False.
    """
    from kubernetes import client as k8s_client

    api = k8s_client.CustomObjectsApi(k8s_clients[0].api_client)
    try:
        api.list_namespaced_custom_object(
            group=_SESSION_CRD_GROUP,
            version=_SESSION_CRD_VERSION,
            namespace=NAMESPACE,
            plural=_SESSION_CRD_PLURAL,
            limit=1,
        )
        print(
            f"\n[CRD] AssistantSession CRD available in namespace {NAMESPACE} ✓",
        )
        return True
    except Exception as exc:
        print(
            f"\n[CRD] AssistantSession CRD not available in namespace "
            f"{NAMESPACE} ({type(exc).__name__}); session assertions will be skipped",
        )
        return False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _assistant_exists_on_orchestra(agent_id: str) -> bool:
    resp = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params={"agent_id": agent_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=10,
    )
    if resp.status_code == 404:
        return False
    if resp.status_code == 200:
        return bool((resp.json().get("info") or []))
    return False


def _get_cleanup_tasks(agent_id: str) -> list[dict]:
    """Query the admin GET endpoint for AssistantCleanupTask rows."""
    resp = requests.get(
        f"{ORCHESTRA_URL}/admin/cleanup/assistant-runtime",
        params={"assistant_id": agent_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=15,
    )
    if resp.status_code == 200:
        return resp.json().get("tasks", [])
    return []


def _redrive_cleanup_tasks() -> None:
    """Nudge Orchestra to process pending AssistantCleanupTask rows.

    DELETE schedules cleanup as a FastAPI BackgroundTask. On Cloud Run with
    CPU throttling, that task can stall with ``attempt_count=0`` after the
    response returns. The admin process endpoint re-drives the durable queue.
    """
    resp = requests.post(
        f"{ORCHESTRA_URL}/admin/cleanup/assistant-runtime",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=60,
    )
    assert (
        resp.status_code == 200
    ), f"cleanup redrive failed: {resp.status_code} {resp.text}"


def _cleanup_task_completed(agent_id: str) -> bool:
    if any(t.get("status") == "completed" for t in _get_cleanup_tasks(agent_id)):
        return True
    _redrive_cleanup_tasks()
    return any(t.get("status") == "completed" for t in _get_cleanup_tasks(agent_id))


def _topic_exists(publisher, topic_path: str) -> bool:
    try:
        publisher.get_topic(request={"topic": topic_path})
        return True
    except Exception:
        return False


def _secret_exists(core_api, secret_name: str) -> bool:
    try:
        core_api.read_namespaced_secret(name=secret_name, namespace=NAMESPACE)
    except ApiException as exc:
        if exc.status == 404:
            return False
        raise
    return True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_delete_assistant_response_is_fast(
    batch_api,
    comms,
    poll,
):
    """DELETE /v0/assistant/{id} must return within DELETE_RESPONSE_TIMEOUT_SECONDS.

    Core UX regression test: if the background task approach is working,
    the user's spinner disappears in ~1 s instead of 20-30 s.
    Works in all environments — no CRD dependency.
    """
    assistant = _create_test_assistant(int(time.time() * 1000) % 1000000)
    agent_id = str(assistant["assistant_id"])

    try:
        t0 = time.monotonic()
        resp = requests.delete(
            f"{ORCHESTRA_URL}/assistant/{agent_id}",
            headers={"Authorization": f"Bearer {UNIFY_KEY}"},
            timeout=60,
        )
        elapsed = time.monotonic() - t0

        assert (
            resp.status_code == 200
        ), f"DELETE /assistant/{agent_id} returned {resp.status_code}: {resp.text}"
        assert elapsed < DELETE_RESPONSE_TIMEOUT_SECONDS, (
            f"DELETE took {elapsed:.1f}s — expected < {DELETE_RESPONSE_TIMEOUT_SECONDS}s. "
            f"Background task may not be working."
        )
        print(
            f"\n[Timing] DELETE /assistant/{agent_id} returned in {elapsed:.2f}s "
            f"(limit: {DELETE_RESPONSE_TIMEOUT_SECONDS}s) ✓",
        )
        assert not _assistant_exists_on_orchestra(
            agent_id,
        ), f"Assistant {agent_id} still visible on Orchestra after 200 DELETE"

    finally:
        _delete_test_assistant(agent_id, batch_api)


@pytest.mark.merge_gate
def test_delete_assistant_runtime_cleanup_completes(
    batch_api,
    gce_client,
    comms,
    pubsub_publisher,
    has_session_crd,
    poll,
):
    """After deletion, the full background cleanup chain must complete.

    Starts a real K8s job so a pool VM is assigned, then deletes the
    assistant and verifies every resource is freed by the background task.

    Assertions (all environments):
      - DELETE response < DELETE_RESPONSE_TIMEOUT_SECONDS
      - AssistantCleanupTask row visible immediately in the DB
      - K8s job stopped
      - Pool VM released back to idle
      - Pub/Sub topic deleted
      - AssistantCleanupTask reaches 'completed'

    Additional assertion (when AssistantSession CRD is available):
      - AssistantSession object deleted
    """
    assistant = _create_test_assistant(int(time.time() * 1000) % 1000000)
    agent_id = str(assistant["assistant_id"])
    topic_name = f"unity-{agent_id}{_PUBSUB_SUFFIX}"
    topic_path = pubsub_publisher.topic_path(GCP_PROJECT_ID, topic_name)

    try:
        assert _topic_exists(
            pubsub_publisher,
            topic_path,
        ), f"Pub/Sub topic {topic_name} was not created during assistant setup"
        print(f"\n[Setup] Pub/Sub topic {topic_name} confirmed ✓")

        has_gce = gce_client is not None
        if has_gce:
            # Clear quarantined VMs so cold-start replenish can reuse pool slots.
            purge_quarantined_pool_vms(comms, vm_type="ubuntu")

        start_real_job(comms, assistant)

        poll(
            lambda: bool(list_jobs_with_assistant_id(batch_api, agent_id)),
            timeout=60,
            interval=5,
            description=f"K8s job for assistant {agent_id} to appear",
        )
        print(f"[Setup] K8s job confirmed running for assistant {agent_id} ✓")

        if has_gce:
            # With POOL_TARGET_IDLE=0, assignment waits on cold-start replenish
            # (provision + boot) rather than claiming a warm idle VM.
            poll(
                lambda: bool(list_assigned_vms(gce_client, agent_id)),
                timeout=600,
                interval=15,
                description=f"Pool VM to be assigned to assistant {agent_id}",
            )
            print(f"[Setup] Pool VM assigned to assistant {agent_id} ✓")
        else:
            print("[Setup] GCE client unavailable — skipping VM assignment wait")

        if has_session_crd:
            session_name = assistant_session_name(agent_id)
            poll(
                lambda: get_assistant_session(comms, agent_id) is not None,
                timeout=120,
                interval=5,
                description=f"AssistantSession for {agent_id} to appear",
            )
            print(f"[Setup] AssistantSession confirmed ✓")
        else:
            session_name = None

        # DELETE — must be fast.
        t0 = time.monotonic()
        resp = requests.delete(
            f"{ORCHESTRA_URL}/assistant/{agent_id}",
            headers={"Authorization": f"Bearer {UNIFY_KEY}"},
            timeout=60,
        )
        elapsed = time.monotonic() - t0

        assert (
            resp.status_code == 200
        ), f"DELETE returned {resp.status_code}: {resp.text}"
        assert (
            elapsed < DELETE_RESPONSE_TIMEOUT_SECONDS
        ), f"DELETE took {elapsed:.1f}s — expected < {DELETE_RESPONSE_TIMEOUT_SECONDS}s"
        print(f"[Timing] DELETE returned in {elapsed:.2f}s ✓")

        # AssistantCleanupTask must be enqueued immediately.
        tasks = _get_cleanup_tasks(agent_id)
        assert tasks, (
            f"No AssistantCleanupTask found for assistant {agent_id} "
            f"immediately after DELETE"
        )
        print(
            f"[Cleanup] AssistantCleanupTask #{tasks[0]['id']} enqueued "
            f"(status={tasks[0]['status']}) ✓",
        )

        wait_for_assistant_runtime_stopped(
            agent_id,
            batch_api=batch_api,
            timeout=CLEANUP_TIMEOUT_SECONDS,
            interval=5,
        )
        print(f"[Cleanup] Assistant runtime released ✓")

        if has_session_crd:
            poll(
                lambda: get_assistant_session(comms, agent_id) is None,
                timeout=CLEANUP_TIMEOUT_SECONDS,
                interval=5,
                description=f"AssistantSession for {agent_id} to be deleted",
                failure_snapshot=lambda: get_assistant_session(comms, agent_id),
            )
            print(f"[Cleanup] AssistantSession deleted ✓")

        # Pub/Sub topic deleted.
        poll(
            lambda: not _topic_exists(pubsub_publisher, topic_path),
            timeout=CLEANUP_TIMEOUT_SECONDS,
            interval=5,
            description=f"Pub/Sub topic {topic_name} to be deleted",
            failure_snapshot=lambda: {
                "topic_exists": _topic_exists(pubsub_publisher, topic_path),
                "cleanup_tasks": _get_cleanup_tasks(agent_id),
            },
        )
        print(f"[Cleanup] Pub/Sub topic deleted ✓")

        # AssistantCleanupTask completed.
        poll(
            lambda: _cleanup_task_completed(agent_id),
            timeout=CLEANUP_TIMEOUT_SECONDS,
            interval=5,
            description=f"AssistantCleanupTask for {agent_id} to reach 'completed'",
            failure_snapshot=lambda: _get_cleanup_tasks(agent_id),
        )
        print(f"[Cleanup] AssistantCleanupTask completed ✓")

    finally:
        _delete_test_assistant(agent_id, batch_api)


@pytest.mark.slow
def test_delete_session_endpoint_tears_down_pending_runtime(
    comms,
    batch_api,
    core_api,
    gce_client,
    has_session_crd,
    job_tracker,
    poll,
):
    """DELETE /infra/session/{id} must tear down the pending K8s runtime.

    Starts a real job, waits for the AssistantSession to reach a pending
    phase (ContainerAssigned / PendingContainer / PendingVM), then calls
    the session delete endpoint and verifies all resources are cleaned up:
    session object, bootstrap secret, K8s job, pool VM.

    Automatically skipped when the AssistantSession CRD is not deployed
    on the target cluster.
    """
    if not has_session_crd:
        pytest.skip(
            f"AssistantSession CRD not available in namespace {NAMESPACE} — "
            f"skipping session teardown test",
        )

    assistant = _create_test_assistant(int(time.time() * 1000) % 1000000)
    assistant_id = str(assistant["assistant_id"])

    try:
        start_real_job(comms, assistant)

        session = poll(
            lambda: (
                candidate
                if (
                    (candidate := get_assistant_session(comms, assistant_id))
                    and (
                        (candidate.get("status") or {}).get("phase")
                        in {
                            "ContainerAssigned",
                            "PendingContainer",
                            "PendingVM",
                        }
                    )
                )
                else None
            ),
            timeout=180,
            interval=5,
            description=f"AssistantSession {assistant_id} to reach a pending phase",
            failure_snapshot=lambda: get_assistant_session(comms, assistant_id),
        )
        assert session is not None
        secret_name = str((session.get("spec") or {}).get("startupSecretRef") or "")
        assert secret_name, f"Expected startupSecretRef on session, got: {session}"

        job_name = (
            (((session.get("status") or {}).get("binding") or {}).get("jobRef") or {})
        ).get("name")
        assert job_name, f"Expected jobRef in pending session, got: {session}"
        job_tracker.track(job_name)

        delete_resp = comms.delete(f"/infra/session/{assistant_id}")
        assert (
            delete_resp.status_code == 200
        ), f"session delete failed: {delete_resp.status_code} {delete_resp.text}"
        assert delete_resp.json()["deleted"] is True

        wait_for_assistant_runtime_stopped(
            assistant_id,
            batch_api=batch_api,
            timeout=240,
            interval=5,
        )

        poll(
            lambda: (
                get_assistant_session(comms, assistant_id) is None
                and not _secret_exists(core_api, secret_name)
            ),
            timeout=240,
            interval=5,
            description=f"AssistantSession record cleanup for {assistant_id}",
            failure_snapshot=lambda: {
                "session": get_assistant_session(comms, assistant_id),
                "secret_exists": _secret_exists(core_api, secret_name),
                "runtime_status": _read_runtime_status_http(assistant_id),
            },
        )
    finally:
        _delete_test_assistant(assistant_id, batch_api)
