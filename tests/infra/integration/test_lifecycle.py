"""
Integration tests for the container lifecycle: idle -> live -> done.

Tests run against real staging K8s. Each test creates its own Job and cleans
up afterward, even on failure.

Invariants covered: INV-1, INV-2, INV-3, INV-5, INV-6
"""

import pytest

from .conftest import (
    NAMESPACE,
    count_idle_jobs,
    expire_test_assistant_records,
    get_job_labels,
    list_jobs_with_assistant_id,
    start_real_job,
)

pytestmark = [pytest.mark.staging]


@pytest.mark.invariant("INV-3", "INV-5")
def test_idle_job_creation(comms, batch_api, job_tracker, poll):
    """Creating a Job via the Comms App produces an idle container with correct labels.

    Verifies:
    - Job is created with unity-status=idle
    - Job has no assistant-id (INV-3)
    - Idle pool capacity is maintained (INV-5)
    """
    resp = comms.post("/infra/job/create", data={"namespace": NAMESPACE})
    assert resp.status_code == 200, f"Job creation failed: {resp.text}"

    result = resp.json()
    job_name = result["job_name"]
    job_tracker.track(job_name)

    labels = poll(
        lambda: get_job_labels(batch_api, job_name),
        timeout=60,
        interval=3,
        description=f"Job {job_name} labels to be readable",
    )

    assert (
        labels.get("unity-status") == "idle"
    ), f"Expected idle, got {labels.get('unity-status')}"
    assert (
        labels.get("assistant-id", "") == ""
    ), f"Idle Job should not have assistant-id, got {labels.get('assistant-id')}"
    assert labels.get("app") == "unity"

    idle = count_idle_jobs(batch_api)
    assert idle >= 1, f"Idle pool should have capacity, got {idle}"


@pytest.mark.invariant("INV-1", "INV-2")
def test_startup_transition_sets_labels(
    comms,
    batch_api,
    job_tracker,
    test_id,
    real_assistant_data,
    poll,
):
    """Calling /infra/job/start with real assistant data causes an idle
    container to transition to live with correct K8s labels.

    Uses the exact same data the adapter sends in production (fetched
    from Orchestra's /admin/assistant endpoint).

    Verifies:
    - Exactly one Job gets the assistant-id (INV-1)
    - The Job's unity-status becomes 'running' (INV-2)
    """
    assistant_id = test_id
    start_real_job(comms, real_assistant_data)
    print(f"\nTriggered job/start for assistant {assistant_id}")

    try:
        matching_jobs = poll(
            lambda: list_jobs_with_assistant_id(batch_api, assistant_id),
            timeout=180,
            interval=10,
            description=f"Job with assistant-id={assistant_id} (container init takes ~35s + label update)",
        )

        assert len(matching_jobs) == 1, (
            f"Expected exactly 1 Job with assistant-id={assistant_id}, "
            f"got {len(matching_jobs)}: {[j.metadata.name for j in matching_jobs]}"
        )

        job_name = matching_jobs[0].metadata.name
        job_tracker.track(job_name)

        labels = get_job_labels(batch_api, job_name)
        assert (
            labels.get("unity-status") == "running"
        ), f"Expected unity-status=running, got {labels.get('unity-status')}"
        assert labels.get("assistant-id") == assistant_id.lower().replace("_", "-")
    finally:
        expire_test_assistant_records(assistant_id)


@pytest.mark.invariant("INV-5")
def test_idle_pool_has_capacity(batch_api):
    """The staging idle pool must always have at least one idle container.

    This is a basic health check — if this fails, the staging pool
    is exhausted and no new assistant sessions can start.
    """
    idle = count_idle_jobs(batch_api)
    assert idle >= 1, (
        f"Idle pool is exhausted: {idle} idle containers. "
        "New assistant sessions will experience cold-start delays."
    )
