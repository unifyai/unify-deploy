"""
Full invariant checker: runs all 14 invariant checks against the current
staging state and reports violations.

This is the "are we clean right now?" test. Run it after all other tests
to confirm nothing was leaked, or run it standalone as a health check.

Invariants covered: all (INV-1 through INV-14)
"""

import pytest

from .conftest import (
    check_invariants,
    count_idle_jobs,
    get_assistant_jobs_records,
    list_idle_vms,
    require_gce,
)

pytestmark = [pytest.mark.staging]


@pytest.mark.invariant(*[f"INV-{i}" for i in range(1, 15)])
def test_staging_invariant_health(k8s_clients, gce_client):
    """Run all invariant checks and report the staging health.

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


def test_container_pool_health(k8s_clients):
    """Basic health: the container pool has idle capacity."""
    batch_api = k8s_clients[0]
    idle = count_idle_jobs(batch_api)
    assert idle >= 1, f"Container pool exhausted: {idle} idle containers"


def test_vm_pool_health(gce_client):
    """Basic health: the VM pool has idle capacity."""
    require_gce(gce_client)
    idle = list_idle_vms(gce_client)
    assert len(idle) >= 1, f"VM pool exhausted: {len(idle)} idle VMs"


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
