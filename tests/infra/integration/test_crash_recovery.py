"""
Integration tests for crash recovery: verifying the job-watcher catches
pod terminations and runs cleanup.

Tests run against real staging K8s. Simulates a crash by deleting a pod
directly, then verifies the job-watcher cleans up.

Invariants covered: INV-13, INV-14
"""

import pytest

from .conftest import (
    NAMESPACE,
    expire_test_assistant_records,
    get_assistant_jobs_records,
    list_jobs_with_assistant_id,
    start_real_job,
)

pytestmark = [pytest.mark.staging]


@pytest.mark.invariant("INV-13", "INV-14")
def test_pod_kill_triggers_cleanup(
    comms,
    batch_api,
    core_api,
    job_tracker,
    test_id,
    real_assistant_data,
    poll,
):
    """Killing a live container's pod triggers job-watcher cleanup.

    Scenario:
    1. Call /infra/job/start to transition a container to live
    2. Wait for it to claim the assistant-id
    3. Delete the pod directly (simulating OOM/crash)
    4. Verify: AssistantJobs record eventually set to running=False (INV-13)

    The job-watcher should detect the Job completion/failure and call
    expire_assistant_records().
    """
    assistant_id = test_id

    try:
        start_real_job(comms, real_assistant_data)

        matching_jobs = poll(
            lambda: list_jobs_with_assistant_id(batch_api, assistant_id),
            timeout=180,
            interval=10,
            description=f"Job with assistant-id={assistant_id} (container init ~35s + label update)",
        )
        assert matching_jobs, f"No Job claimed assistant-id={assistant_id}"

        job = matching_jobs[0]
        job_name = job.metadata.name
        job_tracker.track(job_name)

        pods = core_api.list_namespaced_pod(
            namespace=NAMESPACE,
            label_selector=f"job-name={job_name}",
        )
        assert pods.items, f"No pods found for Job {job_name}"
        pod_name = pods.items[0].metadata.name

        core_api.delete_namespaced_pod(
            name=pod_name,
            namespace=NAMESPACE,
        )

        def check_not_running():
            records = get_assistant_jobs_records(assistant_id, running_only=True)
            return len(records) == 0

        poll(
            check_not_running,
            timeout=120,
            interval=10,
            description=f"AssistantJobs running=False for {assistant_id} (job-watcher cleanup)",
        )

    finally:
        expire_test_assistant_records(assistant_id)


@pytest.mark.invariant("INV-13")
def test_no_orphaned_records_for_test_assistants():
    """No AssistantJobs records should remain running=True for test assistants.

    Test assistant IDs are in the 900_000_000+ range to avoid collisions
    with real assistants (which have low sequential IDs like 82, 479).
    """
    records = get_assistant_jobs_records("9", running_only=True)
    test_records = [
        r
        for r in records
        if int(r.get("entries", {}).get("assistant_id", "0")) >= 900_000_000
    ]

    assert len(test_records) == 0, (
        f"Found {len(test_records)} orphaned running=True records for test assistants: "
        f"{[r.get('entries', {}).get('assistant_id') for r in test_records]}"
    )
