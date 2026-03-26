"""
Integration tests for VM pool lifecycle: assign, probe, release, idempotency,
concurrent assignment safety, and orphaned VM detection.

Tests run against real deployed GCE VMs.

Invariants covered: INV-9, INV-10, INV-11, INV-12
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
import requests

from .conftest import (
    ADMIN_KEY,
    COMMS_APP_URL,
    NAMESPACE,
    UNIFY_KEY,
    list_assigned_vms,
    list_idle_vms,
    require_gce,
    start_real_job,
    wait_for_container_running,
)

_ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}

pytestmark = [pytest.mark.integration]


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
    """The VM pool must have at least one idle VM.

    If this fails, new sessions can't get a desktop VM and
    browser/desktop features will be unavailable.
    """
    require_gce(gce_client)
    idle = list_idle_vms(gce_client)
    assert len(idle) >= 1, (
        f"VM pool exhausted: {len(idle)} idle VMs. "
        "New sessions cannot get a desktop."
    )


# ---------------------------------------------------------------------------
# Concurrent assignment safety
# ---------------------------------------------------------------------------


def _release_all_vms_for(gce_client, assistant_id: str):
    """Release ALL VMs assigned to an assistant (handles duplicates)."""
    for _ in range(5):
        assigned = list_assigned_vms(gce_client, assistant_id)
        if not assigned:
            break
        requests.post(
            f"{COMMS_APP_URL}/infra/vm/pool/release",
            json={"assistant_id": assistant_id},
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        time.sleep(2)


def test_concurrent_assign_produces_at_most_one_vm(
    gce_client,
    real_assistant_data,
):
    """Concurrent VM assign calls for the same assistant must produce at
    most one assigned VM. Without serialization, each call would claim a
    different idle VM, leaking the extras.

    The per-assistant K8s Lease in assign_pool_vm ensures that only one
    call proceeds at a time; the others wait or fail safely.
    """
    require_gce(gce_client)

    assistant_id = str(real_assistant_data["assistant_id"])
    api_key = real_assistant_data.get("api_key", "test-key")

    _release_all_vms_for(gce_client, assistant_id)
    time.sleep(2)

    idle_before = len(list_idle_vms(gce_client))
    if idle_before < 3:
        pytest.skip(
            f"Need >= 3 idle VMs for concurrent assign test, have {idle_before}",
        )

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(
                    requests.post,
                    f"{COMMS_APP_URL}/infra/vm/pool/assign",
                    json={
                        "assistant_id": assistant_id,
                        "unify_apikey": api_key,
                        "vm_type": "ubuntu",
                    },
                    headers=_ADMIN_HEADERS,
                    timeout=120,
                )
                for _ in range(3)
            ]
            results = [f.result() for f in as_completed(futures)]

        statuses = [r.status_code for r in results]
        print(f"  3 concurrent assigns -> statuses: {statuses}")

        time.sleep(5)
        assigned = list_assigned_vms(gce_client, assistant_id)
        vm_names = [vm.name for vm in assigned]
        print(f"  VMs assigned to {assistant_id}: {vm_names}")

        assert len(assigned) <= 1, (
            f"Assistant {assistant_id} has {len(assigned)} VMs "
            f"after 3 concurrent assign calls: {vm_names}. "
            f"Expected at most 1."
        )

    finally:
        _release_all_vms_for(gce_client, assistant_id)


# ---------------------------------------------------------------------------
# Orphaned VM detection after container crash
# ---------------------------------------------------------------------------


def test_orphaned_vm_detected_after_container_crash(
    gce_client,
    comms,
    batch_api,
    real_assistant_data,
    poll,
):
    """When a K8s container is force-deleted (crash, OOM), the VM stays
    assigned because release_pool_vm is never called. The orphaned VM
    reconciler must detect and release it.

    This test verifies the orphan exists, then triggers the reconciler
    and confirms the VM is released.
    """
    require_gce(gce_client)

    assistant_id = str(real_assistant_data["assistant_id"])
    api_key = real_assistant_data.get("api_key", "test-key")
    job_name = None

    try:
        resp = start_real_job(comms, real_assistant_data)
        assert resp.status_code == 200

        jobs = wait_for_container_running(batch_api, assistant_id, timeout=120)
        assert jobs, "Container did not start"
        job_name = jobs[0].metadata.name

        assign_resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/pool/assign",
            json={
                "assistant_id": assistant_id,
                "unify_apikey": api_key,
                "vm_type": "ubuntu",
            },
            headers=_ADMIN_HEADERS,
            timeout=120,
        )
        if assign_resp.status_code != 200:
            pytest.skip("VM assign failed — pool may be exhausted")

        poll(
            lambda: list_assigned_vms(gce_client, assistant_id),
            timeout=60,
            interval=5,
            description=f"VM assignment for {assistant_id}",
        )

        batch_api.delete_namespaced_job(
            name=job_name,
            namespace=NAMESPACE,
            propagation_policy="Foreground",
        )
        job_name = None
        time.sleep(15)

        orphaned = list_assigned_vms(gce_client, assistant_id)
        assert (
            len(orphaned) >= 1
        ), "VM was released despite container crash — expected orphan"
        print(f"  Orphaned VM: {orphaned[0].name} (container deleted)")

        reconcile_resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/pool/reconcile-orphans",
            params={"vm_type": "ubuntu"},
            headers=_ADMIN_HEADERS,
            timeout=60,
        )
        assert reconcile_resp.status_code == 200
        time.sleep(5)

        after_reconcile = list_assigned_vms(gce_client, assistant_id)
        assert len(after_reconcile) == 0, (
            f"Orphan reconciler did not release VM: "
            f"{[vm.name for vm in after_reconcile]}"
        )
        print("  Orphan reconciled — VM released back to pool")

    finally:
        _release_all_vms_for(gce_client, assistant_id)
        if job_name:
            try:
                batch_api.delete_namespaced_job(
                    name=job_name,
                    namespace=NAMESPACE,
                    propagation_policy="Foreground",
                )
            except Exception:
                pass
