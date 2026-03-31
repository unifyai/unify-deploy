"""
Integration tests for duplicate startup prevention.

Verifies that the K8s Lease-based atomic assignment in /infra/job/start
prevents multiple containers from being assigned to the same assistant,
even under concurrent requests.

Invariants covered: INV-1, INV-2
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from .conftest import (
    NAMESPACE,
    expire_test_assistant_records,
    get_assistant_session,
    list_jobs_with_assistant_id,
    list_jobs_with_session_ref,
    start_real_job,
)

pytestmark = [pytest.mark.integration]


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

        session_names = {
            r.get("session_name") for r in responses if r.get("session_name")
        }
        assert len(session_names) == 1, (
            f"Expected exactly 1 session_name across both responses, "
            f"got {len(session_names)}: {session_names}. "
            f"Duplicate session intent was created."
        )

        time.sleep(5)

        session = get_assistant_session(comms, assistant_id)
        assert session is not None
        session_name = session["metadata"]["name"]
        bound_jobs = list_jobs_with_session_ref(batch_api, session_name)
        for job in bound_jobs:
            job_tracker.track(job.metadata.name)

        assert len(bound_jobs) <= 1, (
            f"Expected at most 1 Job with session ref {session_name}, "
            f"got {len(bound_jobs)}: {[j.metadata.name for j in bound_jobs]}. "
            f"Split-brain: two containers serving the same session."
        )

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
        start_real_job(comms, real_assistant_data)
        session = poll(
            lambda: get_assistant_session(comms, assistant_id),
            timeout=120,
            interval=5,
            description=f"AssistantSession for {assistant_id}",
        )
        data = session
        job_name = ((session.get("status") or {}).get("jobRef") or {}).get("name")
        assert job_name, f"Expected jobRef in session status, got: {data}"
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
            labels.get("assistantsession.unify.ai/name") == session["metadata"]["name"]
        ), f"Expected assistantsession label, got labels: {labels}"
        assert (
            annotations.get("assistantsession.unify.ai/name")
            == session["metadata"]["name"]
        ), f"Expected assistantsession annotation, got annotations: {annotations}"

    finally:
        expire_test_assistant_records(assistant_id)
