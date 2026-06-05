"""
Integration coverage for managed desktop assistant parity across VM types.

These tests create real managed assistants in the target environment, wake
them through the adapters service, and assert that both Ubuntu and Windows
reach the same AssistantSession desktop-ready/authenticated VM contract.
"""

import json
import time

import pytest
import requests

from .conftest import (
    ADAPTERS_URL,
    ADMIN_KEY,
    ORCHESTRA_URL,
    UNIFY_KEY,
    _admin_record_to_data,
    _assistant_readiness_snapshot,
    _delete_test_assistant,
    cleanup_assistant_jobs,
    expire_test_assistant_records,
    list_assigned_vms,
    stop_assistant_runtime,
    wait_for_assistant_container_ready,
    wait_for_container_running,
    wait_for_idle_vm_pool,
    wait_for_assistant_runtime_quiesced,
)
from .test_stress import _format_vm_contract_result, _wait_for_assistant_vm_contract

pytestmark = [pytest.mark.integration]


def _wakeup(assistant_id: str):
    """POST /assistant/wakeup and return (response, elapsed_seconds)."""

    t0 = time.monotonic()
    resp = requests.post(
        f"{ADAPTERS_URL}/assistant/wakeup",
        data={"assistant_id": assistant_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    return resp, time.monotonic() - t0


def _create_managed_desktop_test_assistant(desktop_mode: str) -> dict:
    """Create a managed assistant routed to the current integration environment."""

    assert UNIFY_KEY, "UNIFY_KEY required to create managed desktop assistants"
    assert ADMIN_KEY, "ORCHESTRA_ADMIN_KEY required to fetch assistant admin records"

    payload = {
        "first_name": "VmParity",
        "surname": f"{desktop_mode}-{int(time.time()) % 100000}",
        "age": 25,
        "nationality": "North America",
        "about": f"{desktop_mode} managed desktop parity test assistant",
        "desktop_mode": desktop_mode,
        "is_local": False,
        "create_infra": True,
        "timezone": "UTC",
    }
    create_resp = requests.post(
        f"{ORCHESTRA_URL}/assistant",
        json=payload,
        headers={"Authorization": f"Bearer {UNIFY_KEY}"},
        timeout=90,
    )
    assert create_resp.status_code == 200, (
        f"Failed to create {desktop_mode} managed assistant: "
        f"{create_resp.status_code} {create_resp.text}"
    )

    info = create_resp.json().get("info", {})
    agent_id = str(info.get("agent_id", "")).strip()
    assert agent_id, (
        f"No agent_id in {desktop_mode} managed assistant create response: "
        f"{create_resp.text}"
    )

    admin_resp = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params={"agent_id": agent_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    assert admin_resp.status_code == 200, (
        f"Failed to fetch admin record for {desktop_mode} assistant {agent_id}: "
        f"{admin_resp.status_code} {admin_resp.text}"
    )

    admin_info = admin_resp.json()["info"]
    record = admin_info[0] if isinstance(admin_info, list) else admin_info
    return _admin_record_to_data(record)


class TestDesktopVmParity:
    """Managed desktop assistants should satisfy the same VM lifecycle per OS."""

    @pytest.mark.slow
    @pytest.mark.parametrize("desktop_mode", ["ubuntu", "windows"])
    def test_managed_desktop_assistant_reaches_same_vm_contract(
        self,
        desktop_mode,
        batch_api,
        core_api,
        gce_client,
        comms,
    ):
        """Both desktop VM types must reach ready and release cleanly."""

        if gce_client is None:
            pytest.skip("GCE client not available")

        wait_for_idle_vm_pool(
            gce_client,
            min_idle=1,
            vm_type=desktop_mode,
            timeout=240,
        )

        assistant_data = _create_managed_desktop_test_assistant(desktop_mode)
        assistant_id = str(assistant_data["assistant_id"])

        try:
            expire_test_assistant_records(assistant_id)
            cleanup_assistant_jobs(batch_api, [assistant_id])
            time.sleep(5)

            resp, elapsed = _wakeup(assistant_id)
            assert (
                resp.status_code == 200
            ), f"Wakeup failed for {desktop_mode} assistant {assistant_id}: {resp.status_code} {resp.text}"
            assert elapsed < 15.0, (
                f"Wakeup took {elapsed:.1f}s for {desktop_mode} assistant {assistant_id}, "
                "exceeding the 15s budget"
            )

            wait_for_container_running(
                batch_api,
                assistant_id,
                timeout=180,
                interval=10,
            )
            ready_session = wait_for_assistant_container_ready(
                assistant_id,
                batch_api=batch_api,
                core_api=core_api,
                gce_client=gce_client,
                timeout=300,
                interval=5,
            )
            ready_phase = (ready_session.get("status") or {}).get("phase") or ""
            print(
                f"[VMParity] AssistantSession ContainerReady reached for "
                f"{desktop_mode} assistant {assistant_id} "
                f"(phase={ready_phase or 'unknown'}), waiting for desktop-ready/auth contract...",
            )

            result = _wait_for_assistant_vm_contract(
                comms,
                gce_client,
                assistant_data,
                timeout=180,
                interval=10,
                post_assignment_timeout=180,
            )
            snapshot = _assistant_readiness_snapshot(
                assistant_id,
                batch_api=batch_api,
                core_api=core_api,
                gce_client=gce_client,
            )
            assert result.get("state") == "ready", (
                f"{desktop_mode} assistant {assistant_id} did not reach the desktop-ready/auth "
                f"VM contract: {_format_vm_contract_result(result, assignment_timeout=180, readiness_timeout=180)}\n"
                f"VM contract result:\n{json.dumps(result, indent=2, sort_keys=True, default=str)}\n"
                f"Failure snapshot:\n{json.dumps(snapshot, indent=2, sort_keys=True, default=str)}"
            )

            vms = list_assigned_vms(gce_client, assistant_id)
            assert len(vms) == 1, (
                f"Expected exactly 1 assigned VM for {desktop_mode} assistant {assistant_id}, "
                f"got {len(vms)}"
            )
            vm = vms[0]
            labels = dict(vm.labels or {})
            sanitized = assistant_id.lower().replace("_", "-")
            assert labels.get("pool-role") == "assigned", (
                f"Expected pool-role=assigned for {desktop_mode} assistant {assistant_id}, "
                f"got {labels.get('pool-role')}"
            )
            assert labels.get("assistant-id") == sanitized, (
                f"Expected assistant-id={sanitized} for {desktop_mode} assistant {assistant_id}, "
                f"got {labels.get('assistant-id')}"
            )
            assert labels.get("vm-type") == desktop_mode, (
                f"Expected vm-type={desktop_mode} for assistant {assistant_id}, "
                f"got {labels.get('vm-type')}"
            )

            stop_assistant_runtime(
                assistant_id,
                batch_api=batch_api,
                timeout=240,
                strict=True,
                context=f"vm-parity-{desktop_mode}",
            )
            runtime_status = wait_for_assistant_runtime_quiesced(
                assistant_id,
                batch_api=batch_api,
                timeout=240,
                interval=5,
            )
            assert not list_assigned_vms(gce_client, assistant_id), (
                f"Expected no assigned VM after stop for {desktop_mode} assistant "
                f"{assistant_id}"
            )
            assert runtime_status.get("disk_vm_name") is None, (
                f"Expected assistant disk to be detached after stop for "
                f"{desktop_mode} assistant {assistant_id}, got "
                f"{runtime_status.get('disk_vm_name')}"
            )
        finally:
            _delete_test_assistant(assistant_id, batch_api=batch_api)
