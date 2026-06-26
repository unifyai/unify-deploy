"""
Integration tests for cleanup safety: verifying that the resource_version
guard prevents deletion of containers that changed state since the inventory
was fetched.

Tests run against real deployed K8s.

Invariants covered: INV-7, INV-8
"""

import json

import pytest
from kubernetes.client.rest import ApiException

from .conftest import (
    NAMESPACE,
    get_job_labels,
    get_job_resource_version,
)

pytestmark = [pytest.mark.integration]


@pytest.mark.merge_gate
@pytest.mark.invariant("INV-8")
def test_resource_version_guard_prevents_stale_delete(
    comms,
    batch_api,
    job_tracker,
    poll,
):
    """Deleting a Job with a stale resource_version returns 409 Conflict.

    This is the fundamental safety mechanism that prevents cleanup from
    deleting a container that transitioned from idle to live between the
    inventory fetch and the delete call.

    Scenario:
    1. Create an idle Job, record its resource_version
    2. Patch its labels (simulating going live -- changes resource_version)
    3. Attempt delete with the OLD resource_version
    4. Verify 409 Conflict -- the Job survives
    """
    resp = comms.post("/infra/job/create", data={"namespace": NAMESPACE})
    assert resp.status_code == 200
    job_name = resp.json()["job_name"]
    job_tracker.track(job_name)

    poll(
        lambda: get_job_labels(batch_api, job_name),
        timeout=30,
        interval=2,
        description="Job labels readable",
    )

    old_rv = get_job_resource_version(batch_api, job_name)

    comms.patch(
        "/infra/job/labels",
        data={
            "job_name": job_name,
            "labels": json.dumps(
                {"unity-status": "running", "assistant-id": "test-guard"},
            ),
        },
    )

    new_rv = get_job_resource_version(batch_api, job_name)
    assert new_rv != old_rv, "resource_version should change after label patch"

    resp = comms.delete(
        "/infra/job/delete",
        data={
            "job_name": job_name,
            "resource_version": old_rv,
        },
    )

    assert resp.status_code == 409, (
        f"Expected 409 Conflict when deleting with stale resource_version, "
        f"got {resp.status_code}: {resp.text}"
    )

    try:
        job = batch_api.read_namespaced_job(name=job_name, namespace=NAMESPACE)
        assert job is not None, "Job should still exist after failed delete"
    except ApiException as e:
        pytest.fail(f"Job {job_name} was deleted despite stale resource_version: {e}")


@pytest.mark.invariant("INV-8")
def test_delete_with_current_resource_version_succeeds(
    comms,
    batch_api,
    job_tracker,
    poll,
):
    """Deleting a Job with the current resource_version succeeds.

    Confirms the guard allows deletion when the version matches.
    """
    resp = comms.post("/infra/job/create", data={"namespace": NAMESPACE})
    assert resp.status_code == 200
    job_name = resp.json()["job_name"]
    job_tracker.track(job_name)

    poll(
        lambda: get_job_labels(batch_api, job_name),
        timeout=30,
        interval=2,
        description="Job labels readable",
    )

    current_rv = get_job_resource_version(batch_api, job_name)

    resp = comms.delete(
        "/infra/job/delete",
        data={
            "job_name": job_name,
            "resource_version": current_rv,
        },
    )

    assert resp.status_code == 200, (
        f"Expected 200 when deleting with current resource_version, "
        f"got {resp.status_code}: {resp.text}"
    )


@pytest.mark.merge_gate
@pytest.mark.invariant("INV-7", "INV-8")
def test_cleanup_does_not_delete_running_jobs(
    comms,
    batch_api,
    adapters,
    job_tracker,
    poll,
):
    """Running the actual cleanup endpoint preserves Jobs with assistant-id set.

    Creates an idle Job, patches it to running (simulating a live container),
    then triggers cleanup. The Job should survive because:
    1. It has unity-status=running (cleanup targets idle only)
    2. Even if categorized, the resource_version guard would prevent stale delete
    """
    resp = comms.post("/infra/job/create", data={"namespace": NAMESPACE})
    assert resp.status_code == 200
    job_name = resp.json()["job_name"]
    job_tracker.track(job_name)

    poll(
        lambda: get_job_labels(batch_api, job_name),
        timeout=30,
        interval=2,
        description="Job labels readable",
    )

    comms.patch(
        "/infra/job/labels",
        data={
            "job_name": job_name,
            "labels": json.dumps(
                {"unity-status": "running", "assistant-id": "test-cleanup"},
            ),
        },
    )

    cleanup_resp = adapters.post("/scheduled/jobs/cleanup")
    assert cleanup_resp.status_code == 200, f"Cleanup failed: {cleanup_resp.text}"

    try:
        job = batch_api.read_namespaced_job(name=job_name, namespace=NAMESPACE)
        labels = dict(job.metadata.labels or {})
        assert labels.get("assistant-id") == "test-cleanup", (
            f"Job should still have assistant-id=test-cleanup after cleanup, "
            f"got {labels}"
        )
    except ApiException:
        pytest.fail(f"Job {job_name} was deleted by cleanup despite being 'running'")
