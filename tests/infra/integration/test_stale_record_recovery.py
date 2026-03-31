"""
Integration test for stale AssistantJobs record recovery.

Reproduces the production outage scenario where a timed-out
/infra/job/start leaves a running=True record in AssistantJobs
with no corresponding K8s pod. The comms app's /infra/job/start
must start a new container regardless, because it checks K8s
labels (ground truth) for deduplication — AssistantJobs records
are never consulted on the startup path.

The test calls /infra/job/start directly on the comms app (not
through the adapter, since is_local=True assistants skip
auto-start in the adapter routing layer).

Invariants covered: INV-13 (no orphaned AssistantJobs records)
"""

from datetime import datetime, timezone
import time

import pytest
import requests

from .conftest import (
    _create_test_assistant,
    _delete_test_assistant,
    NAMESPACE,
    ORCHESTRA_URL,
    SHARED_KEY,
    cleanup_assistant_jobs,
    count_idle_jobs,
    expire_test_assistant_records,
    list_jobs_with_assistant_id,
    poll_until,
    replenish_pool,
    start_real_job,
    wait_for_idle_pool,
)

pytestmark = [pytest.mark.integration]


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
    comms,
    poll,
):
    """A stale running=True record in AssistantJobs must NOT prevent
    /infra/job/start from creating a container.

    The /infra/job/start endpoint checks K8s labels (ground truth)
    for deduplication — AssistantJobs records are never consulted
    on the startup path.

    We call /infra/job/start directly (not the adapter) because
    is_local=True test assistants skip auto-start in the adapter.
    The invariant under test is the comms app's startup logic, not
    the adapter routing.

    Test sequence:
    1. Create a stale running=True record backdated to 5 min ago
    2. Call /infra/job/start directly on the comms app
    3. Verify: a new container starts (stale record is irrelevant)
    """
    test_assistant = _create_test_assistant(int(time.time()) % 100000)
    assistant_id = str(test_assistant["assistant_id"])
    user_id = test_assistant["user_id"]

    try:
        cleanup_assistant_jobs(batch_api, [assistant_id])
        poll(
            lambda: len(list_jobs_with_assistant_id(batch_api, assistant_id)) == 0,
            timeout=180,
            interval=10,
            description=f"No running Jobs for assistant {assistant_id} before stale-record test",
        )

        record_id = _create_stale_running_record(assistant_id, user_id)
        print(
            f"\n[Stale Record] Created running=True record {record_id} "
            f"for assistant {assistant_id} (no pod exists)",
        )

        if count_idle_jobs(batch_api) == 0:
            replenish_pool()
            wait_for_idle_pool(batch_api, min_idle=1, timeout=120)

        resp = start_real_job(comms, test_assistant)

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
            f"The stale running=True record in AssistantJobs blocked "
            f"the /infra/job/start endpoint."
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
        replenish_pool()
        _delete_test_assistant(assistant_id, batch_api)
