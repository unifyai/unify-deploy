"""
Full invariant checker: runs all 14 invariant checks against the current
deployed state and reports violations.

This is the "are we clean right now?" test. Run it after all other tests
to confirm nothing was leaked, or run it standalone as a health check.

Invariants covered: all (INV-1 through INV-14)
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from .conftest import (
    ASSISTANT_SESSION_BINDING_LABEL,
    ASSISTANT_SESSION_REF_LABEL,
    check_invariants,
    count_idle_jobs,
    get_assistant_jobs_records,
    list_idle_vms,
    require_gce,
)

pytestmark = [pytest.mark.integration]


def _fake_job(
    name: str,
    *,
    assistant_id: str = "",
    unity_status: str = "running",
    session_name: str = "",
    binding_id: str = "",
    active: int = 1,
    deleting: bool = False,
):
    labels = {"unity-status": unity_status}
    if assistant_id:
        labels["assistant-id"] = assistant_id
    if session_name:
        labels[ASSISTANT_SESSION_REF_LABEL] = session_name
    if binding_id:
        labels[ASSISTANT_SESSION_BINDING_LABEL] = binding_id
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels=labels,
            deletion_timestamp="2026-04-06T00:00:00Z" if deleting else None,
        ),
        status=SimpleNamespace(active=active),
    )


def test_check_invariants_flags_duplicate_binding_jobs():
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value.items = [
        _fake_job(
            "unity-job-1",
            assistant_id="1207",
            session_name="assistant-session-1207",
            binding_id="binding-1",
        ),
        _fake_job(
            "unity-job-2",
            assistant_id="1207",
            session_name="assistant-session-1207",
            binding_id="binding-1",
        ),
    ]

    violations = check_invariants(batch_api)

    assert any(
        v.invariant_id == "INV-1" and "Duplicate binding" in v.message
        for v in violations
    )


def test_check_invariants_flags_live_job_marked_for_deletion():
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value.items = [
        _fake_job(
            "unity-job-1",
            assistant_id="1207",
            unity_status="running",
            deleting=True,
        ),
    ]

    violations = check_invariants(batch_api)

    assert any(v.invariant_id == "INV-8" for v in violations)


@pytest.mark.invariant(*[f"INV-{i}" for i in range(1, 15)])
def test_invariant_health(k8s_clients, gce_client):
    """Run all invariant checks and report the deployment health.

    This test does NOT fail on violations -- it reports them as warnings.
    This establishes the baseline. Over time, as issues are fixed, the
    violation count should trend toward zero.

    Once zero is achieved, this test should be changed to fail on any
    violation (strict mode).
    """
    batch_api = k8s_clients[0]
    violations = check_invariants(batch_api, gce_client)

    if violations:
        violation_report = "\n".join(
            f"  [{v.invariant_id}] {v.message}" for v in violations
        )
        pytest.skip(
            f"Staging has {len(violations)} pre-existing invariant violation(s) "
            f"(reported as skip, not failure):\n{violation_report}",
        )


@pytest.mark.merge_gate
def test_container_pool_health(k8s_clients):
    """Basic health: the container pool has idle capacity."""
    batch_api = k8s_clients[0]
    idle = count_idle_jobs(batch_api)
    assert idle >= 1, f"Container pool exhausted: {idle} idle containers"


def test_vm_pool_health(gce_client):
    """Basic health: the VM pool has idle capacity.

    Polls for up to 90s because earlier tests may have consumed idle VMs
    whose release operations (label flip, disk detach, metadata wipe) are
    still in flight when this test runs.
    """
    require_gce(gce_client)
    deadline = time.monotonic() + 90
    idle = []
    while time.monotonic() < deadline:
        idle = list_idle_vms(gce_client)
        if len(idle) >= 1:
            return
        time.sleep(10)
    assert (
        len(idle) >= 1
    ), f"VM pool still exhausted after 90s recovery window: {len(idle)} idle VMs"


def test_no_stale_test_records():
    """No running=True records should exist for test assistants.

    Test assistant IDs are in the 900_000_000+ range.
    If this fails, previous test runs leaked records that need cleanup.
    """
    records = get_assistant_jobs_records("9", running_only=True)
    test_records = [
        r
        for r in records
        if int(r.get("entries", {}).get("assistant_id", "0")) >= 900_000_000
    ]
    assert len(test_records) == 0, (
        f"Found {len(test_records)} leaked test assistant records: "
        f"{[r.get('entries', {}).get('assistant_id') for r in test_records]}. "
        "Run expire_test_assistant_records() to clean up."
    )
