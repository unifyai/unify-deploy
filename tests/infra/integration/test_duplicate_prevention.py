"""
Integration tests for duplicate startup prevention.

Verifies that publishing multiple startup events for the same assistant
results in at most one container serving that assistant.

Invariants covered: INV-1, INV-13
"""

import time

import pytest

from .conftest import (
    NAMESPACE,
    expire_test_assistant_records,
    list_jobs_with_assistant_id,
    start_real_job,
)

pytestmark = [pytest.mark.staging]


@pytest.mark.invariant("INV-1")
def test_concurrent_startups_produce_at_most_one_container(
    comms,
    batch_api,
    job_tracker,
    test_id,
    real_assistant_data,
    poll,
):
    """Calling /infra/job/start twice for the same assistant within 1 second
    results in at most one container serving that assistant.

    This tests the competing-consumer semantics of Pub/Sub (only one subscriber
    per subscription gets each message) combined with the _startup_lock guard
    in CommsManager.
    """
    assistant_id = test_id

    try:
        start_real_job(comms, real_assistant_data)
        time.sleep(0.5)
        start_real_job(comms, real_assistant_data)

        time.sleep(30)

        matching_jobs = list_jobs_with_assistant_id(batch_api, assistant_id)

        for job in matching_jobs:
            job_tracker.track(job.metadata.name)

        assert len(matching_jobs) <= 1, (
            f"Expected at most 1 Job with assistant-id={assistant_id}, "
            f"got {len(matching_jobs)}: {[j.metadata.name for j in matching_jobs]}. "
            f"Split-brain: two containers serving the same assistant."
        )

    finally:
        expire_test_assistant_records(assistant_id)
        all_jobs = batch_api.list_namespaced_job(
            namespace=NAMESPACE,
            label_selector=f"app=unity,assistant-id={assistant_id}",
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


@pytest.mark.invariant("INV-2")
def test_container_labels_set_after_startup(
    comms,
    batch_api,
    job_tracker,
    test_id,
    real_assistant_data,
    poll,
):
    """After a container claims a startup event and goes live, its K8s Job
    labels are set correctly (assistant-id + unity-status=running).

    Uses real assistant data from Orchestra for a fully representative test.
    """
    assistant_id = test_id

    try:
        start_real_job(comms, real_assistant_data)

        matching_jobs = poll(
            lambda: list_jobs_with_assistant_id(batch_api, assistant_id),
            timeout=180,
            interval=10,
            description=f"Job with assistant-id={assistant_id}",
        )

        assert (
            len(matching_jobs) >= 1
        ), f"No container claimed assistant-id={assistant_id}"

        job = matching_jobs[0]
        job_tracker.track(job.metadata.name)

        labels = dict(job.metadata.labels or {})
        assert (
            labels.get("unity-status") == "running"
        ), f"Expected unity-status=running, got {labels.get('unity-status')}"
        assert labels.get("assistant-id") == str(
            assistant_id,
        ), f"Expected assistant-id={assistant_id}, got {labels.get('assistant-id')}"

    finally:
        expire_test_assistant_records(assistant_id)
