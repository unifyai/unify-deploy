"""
Concurrency and stress tests for the deployment infrastructure.

These tests exercise the system under concurrent operations — multiple
startups, cleanup racing with transitions, and pool exhaustion under
burst load — to validate whether the infrastructure handles real-world
concurrency correctly.

The core test suite (test_lifecycle, test_cleanup_safety, etc.) proves
the system works under sequential, low-load conditions. These tests
extend that to concurrent scenarios that arise at scale.

Invariants covered: INV-1, INV-5, INV-8
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
import requests

from .conftest import (
    ADMIN_KEY,
    COMMS_APP_URL,
    NAMESPACE,
    ORCHESTRA_URL,
    UNIFY_KEY,
    cleanup_assistant_jobs,
    count_idle_jobs,
    list_jobs_with_assistant_id,
    poll_until,
    replenish_staging_pool,
    start_real_job,
)

pytestmark = [pytest.mark.staging]


def _fetch_user_assistants(max_count: int = 5) -> list[dict]:
    """Fetch the current user's assistants from Orchestra and build
    startup payloads for each.

    Dynamically discovers assistants so the test works for any team member,
    not just a specific user or hardcoded IDs.
    """
    assert UNIFY_KEY, "UNIFY_KEY required to list assistants"
    assert ADMIN_KEY, "ORCHESTRA_ADMIN_KEY required to fetch assistant details"

    resp = requests.get(
        f"{ORCHESTRA_URL}/assistant",
        headers={"Authorization": f"Bearer {UNIFY_KEY}"},
        timeout=10,
    )
    assert resp.status_code == 200, f"Failed to list assistants: {resp.status_code}"
    data = resp.json()
    all_assistants = data.get("info", data) if isinstance(data, dict) else data

    results = []
    for a_summary in all_assistants[:max_count]:
        agent_id = str(a_summary["agent_id"])
        detail_resp = requests.get(
            f"{ORCHESTRA_URL}/admin/assistant",
            params={"agent_id": agent_id},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=10,
        )
        if detail_resp.status_code != 200:
            continue
        info = detail_resp.json().get("info", [])
        if not info:
            continue
        a = info[0]
        results.append(
            {
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
                "team_ids": "[]",
                "org_id": (
                    str(a.get("organization_id", ""))
                    if a.get("organization_id")
                    else ""
                ),
            },
        )
    return results


# ---------------------------------------------------------------------------
# Scenario: 5 rapid startup calls for the SAME assistant
# ---------------------------------------------------------------------------


@pytest.mark.invariant("INV-1")
def test_burst_startups_same_assistant(
    comms,
    batch_api,
    real_assistant_data,
    poll,
):
    """Fire 5 /infra/job/start calls for the SAME assistant concurrently.

    At most 1 container should claim this assistant. If more than 1 does,
    the user experiences split-brain: messages are split between containers,
    voice calls have duplicate agents, and conversation context is fragmented.

    In production, rapid page refreshes, webhook retries, or multiple inbound
    channels (SMS + email arriving simultaneously) can all trigger multiple
    job/start calls for the same assistant.
    """
    assistant_id = real_assistant_data["assistant_id"]

    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [
                pool.submit(start_real_job, comms, real_assistant_data)
                for _ in range(5)
            ]
            for f in as_completed(futures):
                f.result()

        matching = poll_until(
            lambda: list_jobs_with_assistant_id(batch_api, assistant_id),
            timeout=60,
            interval=10,
            description=f"Jobs with assistant-id={assistant_id} to stabilize",
        )

        assert len(matching) <= 1, (
            f"SPLIT-BRAIN: {len(matching)} containers serving assistant "
            f"{assistant_id} after 5 concurrent startup events. "
            f"Jobs: {[j.metadata.name for j in matching]}. "
            f"At scale, this means duplicate voice agents, split messages, "
            f"and fragmented conversation context."
        )

    finally:
        cleanup_assistant_jobs(batch_api, [assistant_id])
        replenish_staging_pool()


# ---------------------------------------------------------------------------
# Scenario: 3 different users message their assistants at the same time
# ---------------------------------------------------------------------------


@pytest.mark.invariant("INV-1", "INV-5")
def test_simultaneous_startups_different_assistants(comms, batch_api, poll):
    """Start 3 different assistants simultaneously. Each should get exactly
    1 container, and no assistant should get 0 or 2.

    This simulates the real production scenario: multiple users active at
    the same time, each messaging a different assistant. The idle pool must
    serve all of them without split-brain or starvation.

    Dynamically discovers the user's staging assistants so this test works
    for any team member.
    """
    assistants = _fetch_user_assistants(max_count=5)
    assert len(assistants) >= 3, (
        f"Need at least 3 staging assistants, got {len(assistants)}. "
        "Hire more assistants on staging to run this test."
    )
    assistants = assistants[:3]
    used_ids = [a["assistant_id"] for a in assistants]

    idle_before = count_idle_jobs(batch_api)
    assert idle_before >= 3, (
        f"Need >= 3 idle containers for this test, have {idle_before}. "
        "Replenish and retry."
    )

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(start_real_job, comms, a) for a in assistants]
            for f in as_completed(futures):
                f.result()

        time.sleep(60)

        for aid in used_ids:
            matching = list_jobs_with_assistant_id(batch_api, str(aid))
            assert len(matching) == 1, (
                f"Assistant {aid}: expected exactly 1 container, "
                f"got {len(matching)}. "
                f"Jobs: {[j.metadata.name for j in matching]}. "
                + (
                    "Split-brain."
                    if len(matching) > 1
                    else "Starvation — no container claimed it."
                )
            )

    finally:
        cleanup_assistant_jobs(batch_api, used_ids)
        replenish_staging_pool()


# ---------------------------------------------------------------------------
# Scenario: Cleanup runs while a container is mid-transition
# ---------------------------------------------------------------------------


@pytest.mark.invariant("INV-8")
def test_cleanup_during_idle_to_live_transition(
    comms,
    batch_api,
    adapters,
    real_assistant_data,
    poll,
):
    """Start a container's idle-to-live transition, then immediately trigger
    cleanup. The transitioning container must survive.

    This tests the TOCTOU window: cleanup reads the inventory (container is
    idle), sends the delete (container may have gone live). The
    resource_version guard is the defense — if the container's labels changed
    since the inventory read, the delete returns 409 and the container survives.
    """
    assistant_id = real_assistant_data["assistant_id"]

    try:
        start_real_job(comms, real_assistant_data)

        time.sleep(1)
        cleanup_resp = adapters.post("/scheduled/jobs/cleanup")
        assert cleanup_resp.status_code == 200, f"Cleanup failed: {cleanup_resp.text}"

        matching = poll_until(
            lambda: list_jobs_with_assistant_id(batch_api, assistant_id),
            timeout=180,
            interval=10,
            description=(f"Job with assistant-id={assistant_id} surviving cleanup"),
        )

        assert len(matching) >= 1, (
            f"Container for assistant {assistant_id} was killed by cleanup "
            f"during idle-to-live transition. The TOCTOU guard failed."
        )

    finally:
        cleanup_assistant_jobs(batch_api, [assistant_id])
        replenish_staging_pool()


# ---------------------------------------------------------------------------
# Scenario: Rapid start → kill → restart for the same assistant
# ---------------------------------------------------------------------------


@pytest.mark.invariant("INV-1")
def test_rapid_restart_same_assistant(
    comms,
    batch_api,
    core_api,
    real_assistant_data,
    poll,
):
    """Start an assistant, kill its pod (simulating crash), then immediately
    start it again. At most 1 container should be serving at the end.

    This tests the interaction between crash cleanup and new startup.
    With backoffLimit=0, the killed pod should NOT restart. The new startup
    should create a fresh container. At no point should two containers
    serve the same assistant.
    """
    assistant_id = real_assistant_data["assistant_id"]

    try:
        start_real_job(comms, real_assistant_data)

        matching = poll_until(
            lambda: list_jobs_with_assistant_id(batch_api, assistant_id),
            timeout=180,
            interval=10,
            description=f"First container for assistant {assistant_id}",
        )
        assert matching, "No container claimed the first startup"
        first_job_name = matching[0].metadata.name

        pods = core_api.list_namespaced_pod(
            namespace=NAMESPACE,
            label_selector=f"job-name={first_job_name}",
        )
        if pods.items:
            core_api.delete_namespaced_pod(
                name=pods.items[0].metadata.name,
                namespace=NAMESPACE,
            )

        time.sleep(5)

        start_real_job(comms, real_assistant_data)

        time.sleep(60)

        final = list_jobs_with_assistant_id(batch_api, assistant_id)

        assert len(final) <= 1, (
            f"SPLIT-BRAIN during rapid restart: {len(final)} active "
            f"containers for assistant {assistant_id}. "
            f"Jobs: {[j.metadata.name for j in final]}. "
            f"The old container may have restarted (backoffLimit > 0?) "
            f"or the new startup created a duplicate."
        )

    finally:
        cleanup_assistant_jobs(batch_api, [assistant_id])
        replenish_staging_pool()


# ---------------------------------------------------------------------------
# Scenario: Pool exhaustion under burst load
# ---------------------------------------------------------------------------


@pytest.mark.invariant("INV-5")
def test_pool_exhaustion_under_burst(comms, batch_api):
    """Consume multiple idle containers simultaneously, then verify pool
    exhaustion behavior and recovery time.

    Uses different assistants so each startup consumes a separate container.
    Dynamically discovers available assistants and adapts to the current
    pool size.
    """
    assistants = _fetch_user_assistants(max_count=5)

    idle_before = count_idle_jobs(batch_api)
    usable = min(len(assistants), idle_before)
    if usable < 2:
        pytest.skip(
            f"Need >= 2 idle containers and assistants. "
            f"Have {idle_before} idle, {len(assistants)} assistants.",
        )

    to_start = assistants[:usable]
    used_ids = [a["assistant_id"] for a in to_start]

    print(
        f"\n[Pool Exhaustion] Starting with {idle_before} idle containers, "
        f"consuming {usable} simultaneously",
    )

    try:
        with ThreadPoolExecutor(max_workers=usable) as pool:
            futures = [pool.submit(start_real_job, comms, a) for a in to_start]
            for f in as_completed(futures):
                f.result()

        time.sleep(45)

        idle_after = count_idle_jobs(batch_api)
        print(
            f"[Pool Exhaustion] After burst: " f"{idle_after} idle containers remain",
        )

        if idle_after == 0:
            print(
                "[Pool Exhaustion] POOL EXHAUSTED — "
                "next user waits 25-30s for a new container",
            )
            t0 = time.monotonic()
            replenish_staging_pool()
            try:
                poll_until(
                    lambda: count_idle_jobs(batch_api) >= 1,
                    timeout=120,
                    interval=10,
                    description="Pool to recover at least 1 idle container",
                )
                recovery_time = time.monotonic() - t0
                print(
                    f"[Pool Exhaustion] Recovery took {recovery_time:.0f}s",
                )
            except TimeoutError:
                print(
                    "[Pool Exhaustion] WARNING: " "Pool did not recover within 120s",
                )

        for aid in used_ids:
            matching = list_jobs_with_assistant_id(batch_api, str(aid))
            assert len(matching) <= 1, (
                f"Split-brain for assistant {aid}: " f"{len(matching)} containers"
            )

    finally:
        cleanup_assistant_jobs(batch_api, used_ids)
        replenish_staging_pool()


# ---------------------------------------------------------------------------
# Scenario: More simultaneous startups than idle containers (pool overflow)
# ---------------------------------------------------------------------------


def _trigger_reconciliation(comms_client):
    """Trigger the pending-startup reconciler on the comms app."""
    try:
        requests.post(
            f"{COMMS_APP_URL}/infra/pending/process",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
    except Exception:
        pass


def _start_job_tolerant(
    comms_client,
    assistant_data: dict,
    medium: str = "unify_message",
):
    """Like start_real_job but returns (status_code, body) instead of asserting.

    Allows the caller to observe 503 / timeout failures without aborting
    the concurrent burst.
    """
    try:
        resp = comms_client.post(
            "/infra/job/start",
            data={
                "api_key": assistant_data["api_key"],
                "medium": medium,
                **{k: v for k, v in assistant_data.items() if k != "api_key"},
            },
        )
        return resp.status_code, resp.text
    except Exception as e:
        return 0, str(e)


@pytest.mark.invariant("INV-5")
def test_overflow_startups_all_eventually_served(comms, batch_api, poll):
    """Fire MORE /infra/job/start requests than idle containers exist.

    Every assistant must eventually get a container — none should be
    permanently lost due to transient pool exhaustion. The system should
    either queue the overflow requests or retry until new idle containers
    become available via replenishment.

    Reproduces a regression in the K8s Lease-based assignment flow: when
    the pool has N idle containers and N+M requests arrive concurrently,
    M requests get 503 and their startup configs are permanently lost.
    No retry or queuing mechanism exists to fulfil them when new idle
    containers appear via replenishment.
    """
    assistants = _fetch_user_assistants(max_count=10)
    idle_before = count_idle_jobs(batch_api)

    overflow = 2
    burst_size = idle_before + overflow
    if len(assistants) < burst_size:
        pytest.skip(
            f"Need {burst_size} assistants (pool={idle_before} + {overflow} overflow), "
            f"only have {len(assistants)}. Hire more on staging.",
        )

    to_start = assistants[:burst_size]
    used_ids = [a["assistant_id"] for a in to_start]

    print(
        f"\n[Overflow] {burst_size} concurrent requests vs {idle_before} idle containers "
        f"({overflow} will overflow)",
    )

    try:
        # Fire all requests concurrently — some will get 503 or timeout.
        with ThreadPoolExecutor(max_workers=burst_size) as pool:
            futures = {
                pool.submit(_start_job_tolerant, comms, a): a["assistant_id"]
                for a in to_start
            }
            results = {}
            for f in as_completed(futures):
                aid = futures[f]
                status, body = f.result()
                results[aid] = (status, body)
                print(f"  assistant {aid}: HTTP {status}")

        succeeded = [aid for aid, (s, _) in results.items() if s == 200]
        failed = [aid for aid, (s, _) in results.items() if s != 200]
        print(
            f"[Overflow] Immediate results: {len(succeeded)} succeeded, "
            f"{len(failed)} failed",
        )

        # Simulate the production behavior where each webhook triggers
        # replenishment.  In floor regime each call creates 1 container,
        # so we call once per overflow request to ensure enough capacity.
        for _ in range(overflow):
            replenish_staging_pool()

        # Wait for newly created containers to become idle (image pull +
        # boot takes 25-60s), then explicitly trigger reconciliation so
        # the pending messages are assigned.  The 1-minute cron would do
        # this in production, but we don't want the test to depend on
        # wall-clock alignment with the cron schedule.
        poll_until(
            lambda: count_idle_jobs(batch_api) >= overflow,
            timeout=120,
            interval=10,
            description=f"At least {overflow} idle containers for overflow",
        )
        _trigger_reconciliation(comms)

        # The ground truth: check K8s for which assistants actually got
        # containers, regardless of what the HTTP responses said.
        served = []
        lost = []
        for aid in used_ids:
            matching = list_jobs_with_assistant_id(batch_api, str(aid))
            if matching:
                served.append(aid)
            else:
                lost.append(aid)

        print(
            f"[Overflow] Final: {len(served)} served, {len(lost)} lost\n"
            f"  Served: {served}\n"
            f"  Lost:   {lost}",
        )

        assert not lost, (
            f"STARTUP REQUESTS LOST: {len(lost)} of {burst_size} assistants "
            f"never got a container after pool exhaustion.\n"
            f"  Lost assistant IDs: {lost}\n"
            f"  Pool had {idle_before} idle containers for {burst_size} requests.\n"
            f"  The overflow requests were permanently dropped — the system has "
            f"no retry or queuing mechanism for when the pool is exhausted."
        )

    finally:
        cleanup_assistant_jobs(batch_api, used_ids)
        replenish_staging_pool()
