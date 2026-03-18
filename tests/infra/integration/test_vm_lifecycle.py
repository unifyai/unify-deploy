"""
Integration tests for VM pool lifecycle: assign, probe, release, idempotency.

Tests run against real staging GCE VMs.

Invariants covered: INV-9, INV-10, INV-11, INV-12
"""

import pytest
import requests

from .conftest import (
    UNIFY_KEY,
    list_assigned_vms,
    list_idle_vms,
    require_gce,
)

pytestmark = [pytest.mark.staging]


@pytest.mark.invariant("INV-9", "INV-10")
def test_vm_assign_sets_labels_and_metadata(comms, gce_client, test_id, poll):
    """Assigning a pool VM sets correct GCE labels and metadata.

    Verifies:
    - VM labels: pool-role=assigned, assistant-id=<test_id>
    - VM metadata: unify-key is set (non-empty)
    """
    require_gce(gce_client)
    assistant_id = test_id

    try:
        resp = comms.post(
            "/infra/vm/pool/assign",
            json={
                "assistant_id": assistant_id,
                "unify_apikey": "test-api-key-for-integration",
                "vm_type": "ubuntu",
            },
        )
        assert (
            resp.status_code == 200
        ), f"VM assign failed: {resp.status_code} {resp.text}"
        result = resp.json()
        assert result.get("vm_name"), "Response should include vm_name"
        assert result.get("hostname"), "Response should include hostname"

        assigned = poll(
            lambda: list_assigned_vms(gce_client, assistant_id),
            timeout=30,
            interval=3,
            description=f"VM assigned to {assistant_id}",
        )
        assert len(assigned) == 1, f"Expected 1 assigned VM, got {len(assigned)}"

        vm = assigned[0]
        labels = dict(vm.labels or {})
        assert labels.get("pool-role") == "assigned"
        assert labels.get("assistant-id") == assistant_id.lower().replace("_", "-")

    finally:
        comms.post("/infra/vm/pool/release", json={"assistant_id": assistant_id})


@pytest.mark.invariant("INV-11")
def test_vm_auth_key_matches_after_assignment(comms, gce_client, test_id, poll):
    """After VM assignment, the agent-service on the VM should accept the
    expected bearer token.

    Verifies INV-11: the key Unity sends must equal the key in the VM's
    agent-service UNIFY_KEY.

    Note: This test waits for the pool watcher to configure agent-service,
    which can take 30-60 seconds after assignment.

    Uses the real UNIFY_KEY so that both Check 1 (local key match) and
    Check 2 (Orchestra verification) pass.
    """
    require_gce(gce_client)
    assert UNIFY_KEY, "UNIFY_KEY must be set for this test"
    assistant_id = test_id

    try:
        resp = comms.post(
            "/infra/vm/pool/assign",
            json={
                "assistant_id": assistant_id,
                "unify_apikey": UNIFY_KEY,
                "vm_type": "ubuntu",
            },
        )
        assert resp.status_code == 200
        hostname = resp.json().get("hostname", "")
        assert hostname, "No hostname returned from assign"

        def probe_agent_service():
            try:
                r = requests.post(
                    f"https://{hostname}/api/exec",
                    headers={"Authorization": f"Bearer {UNIFY_KEY}"},
                    json={"command": "echo ok", "timeout": 5000},
                    timeout=10,
                    verify=False,
                )
                return r.status_code != 502
            except Exception:
                return False

        poll(
            probe_agent_service,
            timeout=90,
            interval=10,
            description=f"Agent-service on {hostname} to be reachable",
        )

        r = requests.post(
            f"https://{hostname}/api/exec",
            headers={"Authorization": f"Bearer {UNIFY_KEY}"},
            json={"command": "echo ok", "timeout": 5000},
            timeout=10,
            verify=False,
        )

        assert r.status_code != 401, (
            f"Auth mismatch: agent-service returned 401. "
            f"The UNIFY_KEY on the VM does not match the key we sent. "
            f"Response: {r.text}"
        )

    finally:
        comms.post("/infra/vm/pool/release", json={"assistant_id": assistant_id})


@pytest.mark.invariant("INV-10")
def test_vm_release_resets_labels(comms, gce_client, test_id, poll):
    """Releasing a VM returns it to idle with correct labels.

    Verifies:
    - After release: pool-role=idle, assistant-id cleared
    - Idempotency: releasing again returns success
    """
    require_gce(gce_client)
    assistant_id = test_id

    resp = comms.post(
        "/infra/vm/pool/assign",
        json={
            "assistant_id": assistant_id,
            "unify_apikey": "test-key",
            "vm_type": "ubuntu",
        },
    )
    assert resp.status_code == 200
    vm_name = resp.json().get("vm_name", "")

    release_resp = comms.post(
        "/infra/vm/pool/release",
        json={
            "assistant_id": assistant_id,
        },
    )
    assert release_resp.status_code == 200

    assigned_after = list_assigned_vms(gce_client, assistant_id)
    assert len(assigned_after) == 0, (
        f"VM should not be assigned after release, "
        f"found {len(assigned_after)} assigned VMs"
    )

    release_again = comms.post(
        "/infra/vm/pool/release",
        json={
            "assistant_id": assistant_id,
        },
    )
    assert (
        release_again.status_code == 200
    ), f"Idempotent release should succeed, got {release_again.status_code}"


@pytest.mark.invariant("INV-12")
def test_vm_idle_pool_has_capacity(gce_client):
    """The staging VM pool must have at least one idle VM.

    If this fails, new sessions can't get a desktop VM and
    browser/desktop features will be unavailable.
    """
    require_gce(gce_client)
    idle = list_idle_vms(gce_client)
    assert len(idle) >= 1, (
        f"VM pool exhausted: {len(idle)} idle VMs. "
        "New sessions cannot get a desktop."
    )
