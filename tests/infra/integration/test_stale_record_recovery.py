"""
Integration test for stale AssistantJobs record recovery.

Reproduces the production outage scenario where a timed-out
/infra/job/start leaves a running=True record in AssistantJobs
with no corresponding K8s pod. The adapter must start a new
container regardless, because the startup flow bypasses
AssistantJobs entirely — build_webhook_context unconditionally
calls start_unity_job → /infra/job/start, which checks K8s
labels (ground truth) for deduplication.

The test exercises the FULL production code path — no mocks, no
reimplementation. It calls the real adapter webhook endpoint and
verifies that stale Orchestra records have no influence on the
startup decision.

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

    Simulates the residue of a failed startup: the record exists in
    Orchestra but no K8s container was ever created. Backdated by
    age_seconds (default 300s) to represent a realistically stale state.
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
    """A stale running=True record in AssistantJobs must NOT prevent
    new messages from starting a container.

    The adapter's build_webhook_context unconditionally calls
    start_unity_job → /infra/job/start for every valid message.
    The /infra/job/start endpoint checks K8s labels (ground truth)
    for deduplication — AssistantJobs records are never consulted
    on the startup path.

    This means stale records are architecturally irrelevant to the
    startup decision. The test confirms this by creating a stale
    record and verifying that a message still triggers container
    creation.

    Test sequence:
    1. Create a stale running=True record backdated to 5 min ago
    2. Send a real message via the adapter's /unify/message endpoint
    3. Verify: a new container starts (stale record is irrelevant)
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
            f"The stale running=True record in AssistantJobs somehow "
            f"blocked the startup, even though the adapter bypasses "
            f"AssistantJobs entirely and delegates to /infra/job/start."
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
