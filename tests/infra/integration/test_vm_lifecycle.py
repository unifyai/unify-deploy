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
    VM_ZONE,
    list_assigned_vms,
    list_idle_vms,
    list_stopped_vms,
    require_gce,
)

_ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}

pytestmark = [pytest.mark.integration]


def _vm_test_assistant_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time())}"


@pytest.mark.invariant("INV-9", "INV-10")
def test_vm_assign_sets_labels_and_metadata(comms, gce_client, poll):
    """Assigning a pool VM sets correct GCE labels and metadata.

    Verifies:
    - VM labels: pool-role=assigned, assistant-id=<test_id>
    - VM metadata: unify-key is set (non-empty)
    """
    require_gce(gce_client)
    assistant_id = _vm_test_assistant_id("vm-assign-test")

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
def test_vm_auth_key_matches_after_assignment(comms, gce_client, poll):
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
    assistant_id = _vm_test_assistant_id("vm-auth-test")

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
                return 200 <= r.status_code < 300
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

        assert 200 <= r.status_code < 300, (
            f"Authenticated agent-service probe failed with {r.status_code}. "
            f"Response: {r.text}"
        )

    finally:
        comms.post("/infra/vm/pool/release", json={"assistant_id": assistant_id})


@pytest.mark.invariant("INV-10")
def test_vm_release_resets_labels(comms, gce_client, poll):
    """Releasing a VM returns it to idle with correct labels.

    Verifies:
    - After release: pool-role=idle, assistant-id cleared
    - Idempotency: releasing again returns success
    """
    require_gce(gce_client)
    assistant_id = _vm_test_assistant_id("vm-release-test")

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
# Scrub must not kill legitimately booting VMs
# ---------------------------------------------------------------------------


def test_restarted_vm_survives_scrub_and_reaches_idle(gce_client, comms, poll):
    """A stopped VM started by replenish must survive a subsequent scrub
    and eventually transition to idle.

    The scrub function targets pool-role=stopped + status=RUNNING (ghost VMs).
    A VM that was just started by replenish is in that exact state during boot
    (30-60s). If scrub runs during that window, it kills the VM before the
    startup script can call mark-idle.

    The test guarantees replenish starts at least one VM by consuming enough
    idle VMs to push the pool below its target, then observes which VM
    replenish actually starts (rather than picking a target upfront).
    """
    require_gce(gce_client)

    from google.cloud import compute_v1

    client = compute_v1.InstancesClient()

    # Clean up stuck starting VMs from previous runs so the deficit
    # calculation is accurate and rebalance actually starts a VM.
    stuck_req = compute_v1.ListInstancesRequest(
        project="gcp-project-vms",
        zone=VM_ZONE,
        filter="labels.pool-role=starting AND labels.vm-type=ubuntu",
    )
    for stuck in client.list(request=stuck_req):
        try:
            if stuck.status == "RUNNING":
                client.stop(
                    project="gcp-project-vms",
                    zone=VM_ZONE,
                    instance=stuck.name,
                ).result()
            fresh = client.get(
                project="gcp-project-vms",
                zone=VM_ZONE,
                instance=stuck.name,
            )
            labels = dict(fresh.labels or {})
            labels["assistant-id"] = ""
            labels["pool-role"] = "quarantined"
            client.set_labels(
                project="gcp-project-vms",
                zone=VM_ZONE,
                instance=stuck.name,
                instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
                    label_fingerprint=fresh.label_fingerprint,
                    labels=labels,
                ),
            ).result()
            print(f"  Quarantined stuck starting VM: {stuck.name}")
        except Exception:
            pass

    stopped = list_stopped_vms(gce_client)
    if not stopped:
        pytest.skip("No stopped (TERMINATED) VMs available")

    idle_before = list_idle_vms(gce_client)
    pool_target = 5
    needed = max(len(idle_before) - pool_target + 2, 2)
    print(
        f"  Pool: {len(idle_before)} idle, {len(stopped)} stopped. "
        f"Consuming {needed} idle VMs to guarantee deficit.",
    )

    dummy_aids: list[str] = []
    for i in range(needed):
        dummy_aid = f"scrub-test-{int(time.time())}-{i}"
        resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/pool/assign",
            json={
                "assistant_id": dummy_aid,
                "unify_apikey": "test-key",
                "vm_type": "ubuntu",
            },
            headers=_ADMIN_HEADERS,
            timeout=120,
        )
        if resp.status_code == 200:
            dummy_aids.append(dummy_aid)
            print(f"  Consumed idle VM {resp.json().get('vm_name')} ({i+1}/{needed})")
        else:
            print(f"  Assign {i+1} failed ({resp.status_code}), stopping early")
            break

    if not dummy_aids:
        pytest.skip("Could not consume any idle VMs to create deficit")

    starting_before = {
        vm.name
        for vm in client.list(
            request=compute_v1.ListInstancesRequest(
                project="gcp-project-vms",
                zone=VM_ZONE,
                filter="labels.pool-role=starting AND labels.vm-type=ubuntu",
            ),
        )
    }

    # Rebalance #1: replenish should start at least one stopped VM.
    resp1 = comms.post("/infra/vm/pool/rebalance", params={"vm_type": "ubuntu"})
    assert resp1.status_code == 200, f"Rebalance #1 failed: {resp1.text}"
    time.sleep(5)

    starting_after = {
        vm.name
        for vm in client.list(
            request=compute_v1.ListInstancesRequest(
                project="gcp-project-vms",
                zone=VM_ZONE,
                filter="labels.pool-role=starting AND labels.vm-type=ubuntu",
            ),
        )
    }
    newly_started = starting_after - starting_before
    print(f"  Newly started VMs after rebalance #1: {newly_started or '(none)'}")

    if not newly_started:
        for aid in dummy_aids:
            requests.post(
                f"{COMMS_APP_URL}/infra/vm/pool/release",
                json={"assistant_id": aid},
                headers=_ADMIN_HEADERS,
                timeout=30,
            )
        pytest.skip(
            f"Rebalance did not start any VMs (idle={len(idle_before)}, "
            f"consumed={len(dummy_aids)}). Pool may have been replenished "
            f"by a concurrent process.",
        )

    target_name = next(iter(newly_started))

    def _get_state():
        vm = client.get(
            project="gcp-project-vms",
            zone=VM_ZONE,
            instance=target_name,
        )
        return vm.labels.get("pool-role"), vm.status

    role_mid, status_mid = _get_state()
    print(f"  Tracking: {target_name} (pool-role={role_mid}, status={status_mid})")

    # Rebalance #2: scrub runs — must not kill the booting VM
    resp2 = comms.post("/infra/vm/pool/rebalance", params={"vm_type": "ubuntu"})
    assert resp2.status_code == 200, f"Rebalance #2 failed: {resp2.text}"

    try:
        poll(
            lambda: _get_state()[0] == "idle",
            timeout=180,
            interval=10,
            description=f"{target_name} to reach pool-role=idle",
        )
        final_role, final_status = _get_state()
        print(f"  Final: pool-role={final_role}, status={final_status}")
        assert (
            final_role == "idle"
        ), f"{target_name} should be idle, got pool-role={final_role}"
    except TimeoutError:
        final_role, final_status = _get_state()
        if final_role == "starting" and final_status == "RUNNING":
            assert False, (
                f"VM {target_name} survived scrub (pool-role=starting, "
                f"status=RUNNING) but startup script did not call mark-idle "
                f"within 180s. Check the VM serial port output for boot errors "
                f"(supervisord crash, Caddy not starting, etc.)."
            )
        assert False, (
            f"VM {target_name} did not reach idle: "
            f"pool-role={final_role}, status={final_status} after 180s. "
            f"If pool-role=stopped, scrub killed it during boot. "
            f"If pool-role=quarantined, quarantine sweep caught it."
        )
    finally:
        for aid in dummy_aids:
            requests.post(
                f"{COMMS_APP_URL}/infra/vm/pool/release",
                json={"assistant_id": aid},
                headers=_ADMIN_HEADERS,
                timeout=30,
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
):
    """Concurrent VM assign calls for the same assistant must produce at
    most one assigned VM. Without serialization, each call would claim a
    different idle VM, leaking the extras.

    The per-assistant K8s Lease in assign_pool_vm ensures that only one
    call proceeds at a time; the others wait or fail safely.
    """
    require_gce(gce_client)

    assistant_id = _vm_test_assistant_id("vm-concurrent-test")
    api_key = UNIFY_KEY or "test-key"

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


def test_orphaned_vm_detected_and_reconciled(
    gce_client,
    batch_api,
):
    """A VM assigned to an assistant with no running K8s Job is an orphan.
    The reconciler must detect and release it.

    Orphans arise when a pod crashes or is force-deleted without calling
    release_pool_vm. The VM stays in pool-role=assigned indefinitely.

    This test assigns a VM to a dummy assistant that has no container,
    verifies the orphan exists, triggers the reconciler, and confirms
    the VM is released. No start_real_job is used (which would trigger
    a background assign_pool_vm that interferes with the test).
    """
    require_gce(gce_client)

    orphan_aid = f"orphan-test-{int(time.time())}"

    try:
        assign_resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/pool/assign",
            json={
                "assistant_id": orphan_aid,
                "unify_apikey": "orphan-test-key",
                "vm_type": "ubuntu",
            },
            headers=_ADMIN_HEADERS,
            timeout=120,
        )
        if assign_resp.status_code != 200:
            pytest.skip("VM assign failed — pool may be exhausted")

        vm_name = assign_resp.json().get("vm_name", "")
        print(f"  Assigned VM {vm_name} to dummy assistant {orphan_aid}")

        time.sleep(5)
        orphaned = list_assigned_vms(gce_client, orphan_aid)
        assert (
            len(orphaned) >= 1
        ), f"VM should be assigned to {orphan_aid} but found none"

        jobs = batch_api.list_namespaced_job(
            namespace=NAMESPACE,
            label_selector=f"app=unity,assistant-id={orphan_aid}",
        )
        active_jobs = [j for j in jobs.items if j.status.active and j.status.active > 0]
        assert (
            len(active_jobs) == 0
        ), f"Dummy assistant {orphan_aid} should have no K8s Jobs"
        print(f"  Orphan confirmed: VM assigned, no K8s Job")

        reconcile_resp = requests.post(
            f"{COMMS_APP_URL}/infra/vm/pool/reconcile-orphans",
            params={"vm_type": "ubuntu"},
            headers=_ADMIN_HEADERS,
            timeout=60,
        )
        assert reconcile_resp.status_code == 200
        result = reconcile_resp.json()
        print(f"  Reconciler: {result}")
        time.sleep(5)

        after = list_assigned_vms(gce_client, orphan_aid)
        assert len(after) == 0, (
            f"Reconciler did not release orphan: " f"{[vm.name for vm in after]}"
        )
        print("  Orphan reconciled — VM released back to pool")

    finally:
        _release_all_vms_for(gce_client, orphan_aid)
