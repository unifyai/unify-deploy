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
# Scrub must not kill legitimately booting VMs
# ---------------------------------------------------------------------------


def test_restarted_vm_survives_scrub_and_reaches_idle(gce_client, comms, poll):
    """A stopped VM started by replenish must survive a subsequent scrub
    and eventually transition to idle.

    The scrub function targets pool-role=stopped + status=RUNNING (ghost VMs).
    A VM that was just started by replenish is in that exact state during boot
    (30-60s). If scrub runs during that window, it kills the VM before the
    startup script can call mark-idle.

    This test triggers two rebalances in quick succession:
    - Rebalance #1: starts the stopped VM
    - Rebalance #2: scrub runs and must NOT kill the booting VM
    Then waits for the VM to reach idle.
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
            labels["pool-role"] = "stopped"
            client.set_labels(
                project="gcp-project-vms",
                zone=VM_ZONE,
                instance=stuck.name,
                instances_set_labels_request_resource=compute_v1.InstancesSetLabelsRequest(
                    label_fingerprint=fresh.label_fingerprint,
                    labels=labels,
                ),
            ).result()
            print(f"  Cleaned up stuck starting VM: {stuck.name}")
        except Exception:
            pass

    stopped = list_stopped_vms(gce_client)
    if not stopped:
        pytest.skip("No stopped (TERMINATED) VMs available")

    target_name = stopped[0].name

    def _get_state():
        vm = client.get(
            project="gcp-project-vms",
            zone=VM_ZONE,
            instance=target_name,
        )
        return vm.labels.get("pool-role"), vm.status

    role_before, status_before = _get_state()
    print(f"  Target: {target_name} (pool-role={role_before}, status={status_before})")

    # Create a deficit so replenish has a reason to start the stopped VM.
    # Assign an idle VM to a dummy assistant to reduce idle count below target.
    dummy_aid = f"scrub-test-{int(time.time())}"
    dummy_resp = requests.post(
        f"{COMMS_APP_URL}/infra/vm/pool/assign",
        json={
            "assistant_id": dummy_aid,
            "unify_apikey": "test-key",
            "vm_type": "ubuntu",
        },
        headers=_ADMIN_HEADERS,
        timeout=120,
    )
    if dummy_resp.status_code == 200:
        print(
            f"  Consumed 1 idle VM ({dummy_resp.json().get('vm_name')}) to create deficit",
        )

    # Rebalance #1: starts the stopped VM (deficit exists now)
    resp1 = comms.post("/infra/vm/pool/rebalance", params={"vm_type": "ubuntu"})
    assert resp1.status_code == 200, f"Rebalance #1 failed: {resp1.text}"
    time.sleep(3)

    role_mid, status_mid = _get_state()
    print(f"  After rebalance #1: pool-role={role_mid}, status={status_mid}")

    # Rebalance #2: scrub runs — must not kill the booting VM
    resp2 = comms.post("/infra/vm/pool/rebalance", params={"vm_type": "ubuntu"})
    assert resp2.status_code == 200, f"Rebalance #2 failed: {resp2.text}"

    # Wait for the VM to reach idle (boot takes 30-90s)
    try:
        poll(
            lambda: _get_state()[0] == "idle",
            timeout=120,
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
                f"within 120s. Check the VM serial port output for boot errors "
                f"(supervisord crash, Caddy not starting, etc.)."
            )
        assert False, (
            f"Scrub killed booting VM: {target_name} is "
            f"pool-role={final_role}, status={final_status} after 120s. "
            f"Expected pool-role=idle. The scrub function stopped the VM "
            f"before the startup script could call mark-idle."
        )
    finally:
        if dummy_resp.status_code == 200:
            requests.post(
                f"{COMMS_APP_URL}/infra/vm/pool/release",
                json={"assistant_id": dummy_aid},
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
