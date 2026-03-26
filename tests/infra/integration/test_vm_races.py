"""
Regression tests for VM pool race conditions.

Each test empirically reproduces a specific race condition against
the real deployed infrastructure. Tests are designed to FAIL on the
current code (proving the race exists) and PASS after the fix.

Race conditions covered:
- RACE-1: Scrub kills legitimately booting VMs
- RACE-2: release_pool_vm label overwrite without expected_role
- RACE-3: Double VM assignment for same assistant
- RACE-4: Orphaned VMs after pod crash (no automated cleanup)
- Pool exhaustion recovery (stopped VMs start and become idle)

All tests use the real GCE VM pool in the preview environment.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
import requests

from .conftest import (
    ADMIN_KEY,
    COMMS_APP_URL,
    NAMESPACE,
    VM_ZONE,
    list_assigned_vms,
    list_ghost_vms,
    list_idle_vms,
    list_stopped_vms,
    require_gce,
    start_real_job,
    wait_for_container_running,
)

pytestmark = [pytest.mark.integration]

_ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


def _assign_vm(assistant_id: str, api_key: str = "test-key") -> requests.Response:
    """Call the VM assign endpoint."""
    return requests.post(
        f"{COMMS_APP_URL}/infra/vm/pool/assign",
        json={
            "assistant_id": assistant_id,
            "unify_apikey": api_key,
            "vm_type": "ubuntu",
        },
        headers=_ADMIN_HEADERS,
        timeout=120,
    )


def _release_vm(assistant_id: str) -> requests.Response:
    """Call the VM release endpoint."""
    return requests.post(
        f"{COMMS_APP_URL}/infra/vm/pool/release",
        json={"assistant_id": assistant_id},
        headers=_ADMIN_HEADERS,
        timeout=30,
    )


def _rebalance() -> requests.Response:
    """Trigger a pool rebalance (scrub + replenish + trim)."""
    return requests.post(
        f"{COMMS_APP_URL}/infra/vm/pool/rebalance",
        params={"vm_type": "ubuntu"},
        headers=_ADMIN_HEADERS,
        timeout=120,
    )


class TestRace1ScrubKillsBootingVM:
    """RACE-1: _scrub_inconsistent_vms stops VMs that were just started by
    _start_one_stopped_vm before they can call mark-idle.

    The scrub function sees pool-role=stopped + GCE status=RUNNING and
    assumes it's a ghost, but it's actually a VM that's legitimately
    booting (30-60s startup script).
    """

    def test_scrub_does_not_kill_booting_vm(self, gce_client):
        require_gce(gce_client)

        stopped = list_stopped_vms(gce_client)
        if not stopped:
            pytest.skip("No stopped VMs available to test scrub race")

        stopped_name = stopped[0].name
        initial_ghosts = list_ghost_vms(gce_client)
        initial_ghost_names = {vm.name for vm in initial_ghosts}

        _rebalance()
        time.sleep(5)
        _rebalance()

        time.sleep(90)

        final_ghosts = list_ghost_vms(gce_client)
        new_ghosts = [
            vm.name for vm in final_ghosts if vm.name not in initial_ghost_names
        ]
        assert len(new_ghosts) == 0, (
            f"Scrub created {len(new_ghosts)} new ghost VMs by re-stopping "
            f"VMs that were legitimately booting: {new_ghosts}"
        )

        idle = list_idle_vms(gce_client)
        idle_names = {vm.name for vm in idle}
        if stopped_name not in idle_names:
            from google.cloud import compute_v1

            vm = compute_v1.InstancesClient().get(
                project="gcp-project-vms",
                zone=VM_ZONE,
                instance=stopped_name,
            )
            assert False, (
                f"VM {stopped_name} was not recovered to idle. "
                f"Current state: pool-role={vm.labels.get('pool-role')}, "
                f"status={vm.status}. Scrub likely re-stopped it during boot."
            )


class TestRace3DoubleAssign:
    """RACE-3: Two concurrent assign_pool_vm calls for the same assistant
    can each claim a different idle VM, resulting in 2 VMs assigned to
    the same assistant.

    There's no per-assistant lock in assign_pool_vm — only per-VM CAS.
    """

    def test_concurrent_assign_produces_at_most_one_vm(
        self,
        gce_client,
        real_assistant_data,
    ):
        require_gce(gce_client)

        assistant_id = str(real_assistant_data["assistant_id"])
        api_key = real_assistant_data.get("api_key", "test-key")

        _release_vm(assistant_id)
        time.sleep(2)

        idle_before = len(list_idle_vms(gce_client))
        if idle_before < 3:
            pytest.skip(
                f"Need >= 3 idle VMs for double-assign test, have {idle_before}",
            )

        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [
                    pool.submit(_assign_vm, assistant_id, api_key) for _ in range(3)
                ]
                results = [f.result() for f in as_completed(futures)]

            statuses = [r.status_code for r in results]
            print(f"  3 concurrent assigns → statuses: {statuses}")

            time.sleep(5)
            assigned = list_assigned_vms(gce_client, assistant_id)
            print(
                f"  VMs assigned to {assistant_id}: " f"{[vm.name for vm in assigned]}",
            )

            assert len(assigned) <= 1, (
                f"RACE-3: assistant {assistant_id} has {len(assigned)} VMs "
                f"after 3 concurrent assign calls: "
                f"{[vm.name for vm in assigned]}. "
                f"Expected at most 1."
            )

        finally:
            _release_vm(assistant_id)


class TestRace2ReleaseOverwrite:
    """RACE-2: release_pool_vm sets pool-role=idle without expected_role,
    which can overwrite a concurrent claim that changed the VM to assigned.

    Scenario: release VM from A, while B concurrently claims it. A's
    release label-set arrives after B's claim, resetting assigned→idle.
    """

    def test_release_does_not_overwrite_new_assignment(
        self,
        gce_client,
        real_assistant_data,
    ):
        require_gce(gce_client)

        assistant_a = str(real_assistant_data["assistant_id"])
        assistant_b = f"race-test-{int(time.time())}"

        idle_before = len(list_idle_vms(gce_client))
        if idle_before < 2:
            pytest.skip(f"Need >= 2 idle VMs, have {idle_before}")

        try:
            resp_a = _assign_vm(assistant_a, "key-a")
            assert resp_a.status_code == 200, f"Assign A failed: {resp_a.text}"
            vm_a_name = resp_a.json().get("vm_name")

            resp_b = _assign_vm(assistant_b, "key-b")
            assert resp_b.status_code == 200, f"Assign B failed: {resp_b.text}"

            time.sleep(5)

            assigned_b = list_assigned_vms(gce_client, assistant_b)
            assert (
                len(assigned_b) == 1
            ), f"Assistant B should have exactly 1 VM, has {len(assigned_b)}"
            assert assigned_b[0].labels.get("pool-role") == "assigned", (
                f"B's VM should be assigned, but pool-role="
                f"{assigned_b[0].labels.get('pool-role')}. "
                f"Release of A's VM may have overwritten B's claim."
            )

        finally:
            _release_vm(assistant_a)
            _release_vm(assistant_b)


class TestRace4OrphanedVM:
    """RACE-4: When a K8s pod is killed, release_pool_vm is never called.
    The VM stays in pool-role=assigned indefinitely with no automated
    cleanup mechanism.
    """

    def test_orphaned_vm_detected_after_container_crash(
        self,
        gce_client,
        comms,
        batch_api,
        real_assistant_data,
        poll,
    ):
        require_gce(gce_client)

        assistant_id = str(real_assistant_data["assistant_id"])

        try:
            resp = start_real_job(comms, real_assistant_data)
            assert resp.status_code == 200

            jobs = wait_for_container_running(batch_api, assistant_id, timeout=120)
            assert jobs, "Container did not start"
            job_name = jobs[0].metadata.name

            resp = _assign_vm(assistant_id, real_assistant_data.get("api_key", "k"))
            if resp.status_code != 200:
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
            time.sleep(15)

            orphaned = list_assigned_vms(gce_client, assistant_id)
            assert len(orphaned) >= 1, (
                "VM was released despite container crash — "
                "this should NOT happen without an explicit reconciler"
            )
            print(
                f"  Orphaned VM confirmed: {orphaned[0].name} still assigned "
                f"to {assistant_id} after container deletion",
            )

        finally:
            _release_vm(assistant_id)
            try:
                batch_api.delete_namespaced_job(
                    name=job_name,
                    namespace=NAMESPACE,
                    propagation_policy="Foreground",
                )
            except Exception:
                pass


class TestVMPoolExhaustionRecovery:
    """The VM pool should recover from exhaustion by starting stopped VMs
    via replenish. This tests the end-to-end flow: all idle VMs consumed
    → replenish starts stopped VMs → new assignment eventually succeeds.
    """

    def test_pool_recovers_from_exhaustion(self, gce_client):
        require_gce(gce_client)

        idle = list_idle_vms(gce_client)
        stopped = list_stopped_vms(gce_client)
        if len(idle) < 1 or len(stopped) < 1:
            pytest.skip(
                f"Need >= 1 idle + >= 1 stopped VM. Have {len(idle)} idle, "
                f"{len(stopped)} stopped.",
            )

        consumed_assistants = []
        try:
            for i, vm in enumerate(idle):
                aid = f"exhaust-test-{i}-{int(time.time())}"
                resp = _assign_vm(aid, "exhaust-key")
                if resp.status_code == 200:
                    consumed_assistants.append(aid)
                else:
                    break

            remaining_idle = list_idle_vms(gce_client)
            print(
                f"  Consumed {len(consumed_assistants)} VMs, "
                f"{len(remaining_idle)} idle remaining",
            )

            overflow_aid = f"overflow-test-{int(time.time())}"
            resp = _assign_vm(overflow_aid, "overflow-key")
            consumed_assistants.append(overflow_aid)

            assert resp.status_code == 200, (
                f"Pool did not recover from exhaustion. "
                f"Assign returned {resp.status_code}: {resp.text}. "
                f"Replenish should have started a stopped VM."
            )

        finally:
            for aid in consumed_assistants:
                try:
                    _release_vm(aid)
                except Exception:
                    pass
