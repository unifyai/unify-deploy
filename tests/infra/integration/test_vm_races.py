"""
Regression tests for confirmed VM pool race conditions.

Each test empirically reproduces a specific race condition against
the real deployed infrastructure. Tests clean up all artifacts in
finally blocks to avoid leaking VMs.

Confirmed races:
- RACE-3: Double VM assignment for same assistant (no per-assistant lock)
- RACE-4: Orphaned VMs after pod crash (no automated cleanup)
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
import requests

from .conftest import (
    ADMIN_KEY,
    COMMS_APP_URL,
    NAMESPACE,
    list_assigned_vms,
    list_idle_vms,
    require_gce,
    start_real_job,
    wait_for_container_running,
)

pytestmark = [pytest.mark.integration]

_ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


def _assign_vm(assistant_id: str, api_key: str = "test-key") -> requests.Response:
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
    return requests.post(
        f"{COMMS_APP_URL}/infra/vm/pool/release",
        json={"assistant_id": assistant_id},
        headers=_ADMIN_HEADERS,
        timeout=30,
    )


def _release_all_vms_for(gce_client, assistant_id: str):
    """Release ALL VMs assigned to an assistant, not just the first.

    release_pool_vm only releases one VM per call (LIST returns the first
    match). When RACE-3 produces multiple VMs for the same assistant,
    we must loop until all are released.
    """
    for _ in range(5):
        assigned = list_assigned_vms(gce_client, assistant_id)
        if not assigned:
            break
        _release_vm(assistant_id)
        time.sleep(2)


class TestRace3DoubleAssign:
    """RACE-3: Two or more concurrent assign_pool_vm calls for the same
    assistant can each claim a different idle VM, because there is no
    per-assistant lock in assign_pool_vm.

    CONFIRMED: 3 concurrent assigns → 3 VMs assigned to assistant 854.
    """

    def test_concurrent_assign_produces_at_most_one_vm(
        self,
        gce_client,
        real_assistant_data,
    ):
        require_gce(gce_client)

        assistant_id = str(real_assistant_data["assistant_id"])
        api_key = real_assistant_data.get("api_key", "test-key")

        _release_all_vms_for(gce_client, assistant_id)
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
            print(f"  3 concurrent assigns -> statuses: {statuses}")

            time.sleep(5)
            assigned = list_assigned_vms(gce_client, assistant_id)
            vm_names = [vm.name for vm in assigned]
            print(f"  VMs assigned to {assistant_id}: {vm_names}")

            assert len(assigned) <= 1, (
                f"RACE-3: assistant {assistant_id} has {len(assigned)} VMs "
                f"after 3 concurrent assign calls: {vm_names}. "
                f"Expected at most 1 (no per-assistant lock)."
            )

        finally:
            _release_all_vms_for(gce_client, assistant_id)


class TestRace4OrphanedVM:
    """RACE-4: When a K8s pod is killed, release_pool_vm is never called.
    The VM stays in pool-role=assigned indefinitely.

    CONFIRMED: Baseline invariant violations show assistants 784 and 917
    with assigned VMs but no running K8s Jobs. The stress test showed
    '1047: VM still assigned (orphaned)' after pod crash.
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
        api_key = real_assistant_data.get("api_key", "test-key")
        job_name = None

        try:
            resp = start_real_job(comms, real_assistant_data)
            assert resp.status_code == 200

            jobs = wait_for_container_running(
                batch_api,
                assistant_id,
                timeout=120,
            )
            assert jobs, "Container did not start"
            job_name = jobs[0].metadata.name

            assign_resp = _assign_vm(assistant_id, api_key)
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
            assert len(orphaned) >= 1, (
                "VM was released despite container crash — "
                "expected it to stay orphaned (no automated cleanup)"
            )
            print(
                f"  Orphaned VM confirmed: {orphaned[0].name} still assigned "
                f"to {assistant_id} after container deletion",
            )

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
