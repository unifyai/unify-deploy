"""
Integration test for stale AssistantJobs record recovery.

Reproduces the exact production outage Dan reported on 2026-03-17:
a timed-out /infra/job/start leaves a running=True record in
AssistantJobs with no corresponding K8s pod. Subsequent messages
go through the adapter's build_webhook_context → is_job_running
path and are silently dropped because is_job_running returns True.

The test exercises the FULL production code path — no mocks, no
reimplementation. It calls the real adapter webhook endpoint and
checks whether the system recovers.

Invariants covered: INV-13 (no orphaned AssistantJobs records)
"""

from datetime import datetime, timezone

import pytest
import requests

from .conftest import (
    ADAPTERS_URL,
    ADMIN_KEY,
    NAMESPACE,
    ORCHESTRA_URL,
    SHARED_KEY,
    expire_test_assistant_records,
    list_jobs_with_assistant_id,
    poll_until,
    replenish_staging_pool,
)

pytestmark = [pytest.mark.staging]


def _create_stale_running_record(
    assistant_id: str,
    user_id: str,
    age_seconds: int = 300,
) -> str:
    """Insert a running=True record into AssistantJobs with no corresponding pod.

    This simulates the end state of a failed /infra/job/start call:
    mark_job_running wrote the record, but the startup timed out and
    no container was ever created.

    The record is backdated by age_seconds (default 300s = 5 minutes)
    so it appears old enough to exceed the 120s TTL in the two-phase
    is_job_running check. In the real outage, the record was hours old.
    """
    from datetime import timedelta

    stale_timestamp = (
        datetime.now(tz=timezone.utc) - timedelta(seconds=age_seconds)
    ).isoformat()

    resp = requests.post(
        f"{ORCHESTRA_URL}/logs",
        json={
            "project_name": "AssistantJobs",
            "context": "startup_events",
            "entries": {
                "user_id": user_id,
                "assistant_id": str(assistant_id),
                "running": True,
                "medium": "unify_message",
                "job_name": "phantom-job-never-created",
                "timestamp": stale_timestamp,
            },
        },
        headers={"Authorization": f"Bearer {SHARED_KEY}"},
        timeout=10,
    )
    assert (
        resp.status_code == 200
    ), f"Failed to create stale record: {resp.status_code} {resp.text}"
    return resp.json().get("id", "")


@pytest.mark.invariant("INV-13")
def test_stale_record_does_not_block_new_startup(
    batch_api,
    real_assistant_data,
    poll,
):
    """A stale running=True record older than 120 seconds with no K8s
    pod must NOT prevent new messages from starting a container.

    This reproduces the exact scenario that caused a 7+ hour outage
    on 2026-03-17: a timed-out startup left a stale record, and every
    subsequent message was silently dropped because is_job_running
    returned True based on the stale record.

    The two-phase is_job_running check handles this via:
      Phase 1: K8s query finds no active pod → returns False
      Phase 2: AssistantJobs record is >120s old → treated as stale

    Both phases independently reject the stale record, so the adapter
    proceeds to start a new container.

    The record is backdated to 5 minutes ago (well past the 120s TTL)
    to simulate the production scenario where minutes or hours pass
    between the failed startup and the next user message.

    Test sequence:
    1. Create a stale running=True record backdated to 5 min ago
    2. Send a real message via the adapter's /unify/message endpoint
    3. Verify: a new container starts (the stale record was bypassed)
    """
    assistant_id = str(real_assistant_data["assistant_id"])
    user_id = real_assistant_data["user_id"]

    try:
        existing = list_jobs_with_assistant_id(batch_api, assistant_id)
        assert not existing, (
            f"Precondition failed: {len(existing)} active Job(s) already "
            f"exist for assistant {assistant_id}. Clean up staging first."
        )

        record_id = _create_stale_running_record(assistant_id, user_id)
        print(
            f"\n[Stale Record] Created running=True record {record_id} "
            f"for assistant {assistant_id} (no pod exists)",
        )

        resp = requests.post(
            f"{ADAPTERS_URL}/unify/message",
            json={
                "assistant_id": assistant_id,
                "contact_id": 1,
                "body": "Integration test: message sent with stale record present",
            },
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
        assert (
            resp.status_code == 200
        ), f"Adapter rejected message: {resp.status_code} {resp.text}"

        started = poll_until(
            lambda: list_jobs_with_assistant_id(batch_api, assistant_id),
            timeout=180,
            interval=10,
            description=(
                f"Container for assistant {assistant_id} to start "
                f"despite stale running=True record"
            ),
        )

        assert len(started) >= 1, (
            f"No container started for assistant {assistant_id}. "
            f"The stale running=True record blocked the startup. "
            f"is_job_running returned True despite no K8s pod existing. "
            f"This is the exact failure mode that caused the ClientGamma outage."
        )

        print(f"[Stale Record] Container started: {started[0].metadata.name}")

    finally:
        expire_test_assistant_records(assistant_id)
        all_jobs = batch_api.list_namespaced_job(
            namespace=NAMESPACE,
            label_selector=f"app=unity,assistant-id={assistant_id.lower().replace('_', '-')}",
        )
        for job in all_jobs.items:
            try:
                batch_api.delete_namespaced_job(
                    name=job.metadata.name,
                    namespace=NAMESPACE,
                    propagation_policy="Foreground",
                )
            except Exception:
                pass
        replenish_staging_pool()
