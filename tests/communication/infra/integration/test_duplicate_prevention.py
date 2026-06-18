"""
Integration tests for duplicate startup prevention.

Verifies that the AssistantSession-backed /infra/job/start convergence path
prevents multiple runtimes from being established for the same assistant,
even under concurrent requests.

Invariants covered: INV-1, INV-2
"""

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from .conftest import (
    _create_test_assistant,
    _delete_test_assistant,
    get_assistant_session,
    list_jobs_with_assistant_id,
    list_jobs_with_session_ref,
    replenish_pool,
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

    The concurrent callers may race while ensuring the AssistantSession, but
    they must converge onto the same session/activation instead of creating
    split-brain runtime intent.
    """
    test_assistant = _create_test_assistant(int(time.time() * 1000) % 1000000)
    assistant_id = str(test_assistant["assistant_id"])

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(start_real_job, comms, test_assistant)
            f2 = pool.submit(start_real_job, comms, test_assistant)
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
        session_activation_id = str(
            (session.get("spec") or {}).get("activationId", "") or "",
        )
        response_activation_ids = {
            r.get("activation_id") for r in responses if r.get("activation_id")
        }
        assert response_activation_ids == {session_activation_id}, (
            f"Expected both responses to return canonical activation_id "
            f"{session_activation_id}, got {response_activation_ids}. "
            f"Duplicate callers did not converge on the same runtime intent."
        )
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
        _delete_test_assistant(assistant_id, batch_api)
        replenish_pool()


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
    are set correctly (assistant-id + droid-status=running) and the
    startup config is written as an annotation.

    The labels and annotation are written atomically by the comms app
    (not by the container itself), so they are visible immediately.
    """
    test_assistant = _create_test_assistant(int(time.time() * 1000) % 1000000)
    assistant_id = str(test_assistant["assistant_id"])

    try:
        start_real_job(comms, test_assistant)
        matching_jobs = poll(
            lambda: list_jobs_with_assistant_id(batch_api, assistant_id),
            timeout=180,
            interval=10,
            description=f"Job with assistant-id={assistant_id}",
        )
        assert len(matching_jobs) == 1, (
            f"Expected exactly 1 active Job with assistant-id={assistant_id}, "
            f"got {len(matching_jobs)}: {[j.metadata.name for j in matching_jobs]}"
        )

        job = matching_jobs[0]
        job_name = job.metadata.name
        job_tracker.track(job_name)

        session = poll(
            lambda: get_assistant_session(comms, assistant_id),
            timeout=120,
            interval=5,
            description=f"AssistantSession for {assistant_id}",
        )
        data = session
        session_job_name = (
            (((session.get("status") or {}).get("binding") or {}).get("jobRef") or {})
        ).get("name")
        assert session_job_name == job_name, (
            f"Expected session jobRef to point to {job_name}, got {session_job_name}. "
            f"Session data: {data}"
        )

        labels = dict(job.metadata.labels or {})
        annotations = dict(job.metadata.annotations or {})

        assert (
            labels.get("droid-status") == "running"
        ), f"Expected droid-status=running, got {labels.get('droid-status')}"
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
        _delete_test_assistant(assistant_id, batch_api)
        replenish_pool()
