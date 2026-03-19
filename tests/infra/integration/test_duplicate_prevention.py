"""
Integration tests for duplicate startup prevention.

Verifies that the K8s Lease-based atomic assignment in /infra/job/start
prevents multiple containers from being assigned to the same assistant,
even under concurrent requests.

Invariants covered: INV-1, INV-2
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

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
    """Two concurrent /infra/job/start calls for the same assistant must
    result in at most one container being assigned.

    The Lease-based lock in /infra/job/start serializes concurrent callers:
    only the Lease holder proceeds to claim an idle container. The second
    caller gets 409 on Lease creation and returns early.
    """
    assistant_id = test_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(start_real_job, comms, real_assistant_data)
            f2 = pool.submit(start_real_job, comms, real_assistant_data)
            r1 = f1.result()
            r2 = f2.result()

        responses = [r1.json(), r2.json()]
        print(f"\n[Dedup] Response 1: {responses[0]}")
        print(f"[Dedup] Response 2: {responses[1]}")

        job_names = {r.get("job_name") for r in responses if r.get("job_name")}
        assert len(job_names) <= 1, (
            f"Expected at most 1 unique job_name across both responses, "
            f"got {len(job_names)}: {job_names}. "
            f"The Lease failed to prevent duplicate assignment."
        )

        time.sleep(5)

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
    """After /infra/job/start assigns a container, its K8s Job labels
    are set correctly (assistant-id + unity-status=running) and the
    startup config is written as an annotation.

    The labels and annotation are written atomically by the comms app
    (not by the container itself), so they are visible immediately.
    """
    assistant_id = test_id

    try:
        resp = start_real_job(comms, real_assistant_data)
        data = resp.json()
        assert data.get("job_name"), f"Expected job_name in response, got: {data}"

        job_name = data["job_name"]
        job_tracker.track(job_name)

        job = batch_api.read_namespaced_job(name=job_name, namespace=NAMESPACE)
        labels = dict(job.metadata.labels or {})
        annotations = dict(job.metadata.annotations or {})

        assert (
            labels.get("unity-status") == "running"
        ), f"Expected unity-status=running, got {labels.get('unity-status')}"
        sanitized = str(assistant_id).lower().replace("_", "-")
        assert (
            labels.get("assistant-id") == sanitized
        ), f"Expected assistant-id={sanitized}, got {labels.get('assistant-id')}"
        assert (
            "unity-startup-config" in annotations
        ), f"Expected unity-startup-config annotation, got keys: {list(annotations.keys())}"

        config = json.loads(annotations["unity-startup-config"])
        assert config["assistant_id"] == str(
            assistant_id,
        ), f"Startup config assistant_id mismatch: {config.get('assistant_id')}"

    finally:
        expire_test_assistant_records(assistant_id)
