from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from communication.infra import vm_helpers as vm_helpers_module
from communication.infra.vm_helpers import (
    AssistantDiskInUseError,
    _claim_idle_vm_inner,
    complete_pool_vm_release,
    _is_stale_inflight_vm,
    _quarantine_pool_vm,
    _start_one_stopped_vm,
    assign_pool_vm,
    delete_assistant_disk,
    release_pool_vm,
    replenish_pool,
    split_binding_runtime_vms,
)


def _fake_vm(*, role: str, age_seconds: int, status: str = "RUNNING"):
    started_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return SimpleNamespace(
        labels={"pool-role": role},
        status=status,
        last_start_timestamp=started_at.isoformat(),
        creation_timestamp=started_at.isoformat(),
    )


def _current_contract_labels(**labels):
    return {
        "pool-contract-generation": vm_helpers_module.POOL_VM_CONTRACT_GENERATION,
        **labels,
    }


def _install_binding_lease(monkeypatch, *, acquire_results=None):
    coord_api = object()
    acquire_iter = iter(acquire_results or [True])
    released = []

    monkeypatch.setattr(
        "communication.infra.helpers.setup_kubernetes_client",
        lambda: (None, None, None, coord_api),
    )
    monkeypatch.setattr(
        "communication.infra.helpers.acquire_assignment_lease",
        lambda *_args, **_kwargs: next(acquire_iter),
    )
    monkeypatch.setattr(
        "communication.infra.helpers.release_assignment_lease",
        lambda *_args, **_kwargs: released.append(True),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.time.sleep",
        lambda *_args, **_kwargs: None,
    )
    return released


def test_is_stale_inflight_vm_detects_old_starting_vm():
    vm = _fake_vm(role="starting", age_seconds=1200)
    assert _is_stale_inflight_vm(vm, timeout_seconds=600)


def test_is_stale_inflight_vm_ignores_recent_starting_vm():
    vm = _fake_vm(role="starting", age_seconds=120)
    assert not _is_stale_inflight_vm(vm, timeout_seconds=600)


def test_is_stale_inflight_vm_prefers_explicit_transition_epoch():
    old_started_at = datetime.now(UTC) - timedelta(seconds=1200)
    recent_transition = datetime.now(UTC) - timedelta(seconds=120)
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-6-preview",
        labels={
            "pool-role": "starting",
            "pool-transition-epoch": str(int(recent_transition.timestamp())),
        },
        status="RUNNING",
        last_start_timestamp=old_started_at.isoformat(),
        creation_timestamp=old_started_at.isoformat(),
    )

    assert not _is_stale_inflight_vm(vm, timeout_seconds=600)


def test_is_stale_inflight_vm_ignores_non_inflight_roles():
    vm = _fake_vm(role="stopped", age_seconds=1200, status="TERMINATED")
    assert not _is_stale_inflight_vm(vm, timeout_seconds=600)


def test_start_one_stopped_vm_returns_after_start_request(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-6-preview",
        labels=_current_contract_labels(**{"vm-type": "ubuntu"}),
    )
    client = MagicMock()
    client.start.return_value = SimpleNamespace(
        name="operation-123",
        result=lambda: (_ for _ in ()).throw(AssertionError("should not wait")),
    )
    metadata_updates = []

    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._pool_bootstrap_metadata_updates",
        lambda *_args, **_kwargs: {
            "pool-watcher-script": "watcher",
            "pool-contract-generation": vm_helpers_module.POOL_VM_CONTRACT_GENERATION,
        },
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._update_instance_metadata",
        lambda vm_name, updates, **_kwargs: metadata_updates.append((vm_name, updates)),
    )

    assert _start_one_stopped_vm(client, vm)
    client.start.assert_called_once()
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-6-preview",
            {
                "pool-watcher-script": "watcher",
                "pool-contract-generation": vm_helpers_module.POOL_VM_CONTRACT_GENERATION,
            },
        ),
    ]


def test_claim_idle_vm_does_not_require_agent_service_before_assignment(monkeypatch):
    pool_vm = SimpleNamespace(
        name="unity-pool-ubuntu-2-preview",
        labels=_current_contract_labels(**{"pool-role": "idle", "vm-type": "ubuntu"}),
        label_fingerprint="unity-pool-ubuntu-2-preview-fp",
        network_interfaces=[],
        metadata=SimpleNamespace(
            items=[
                SimpleNamespace(
                    key="hostname",
                    value="unity-pool-ubuntu-2-preview.example.com",
                ),
            ],
        ),
    )
    client = MagicMock()
    client.list.return_value = [pool_vm]
    client.get.return_value = pool_vm
    client.set_labels.return_value = SimpleNamespace(result=lambda: None)

    monkeypatch.setattr(
        "communication.infra.vm_helpers.random.choice",
        lambda vms: vms[0],
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.probe_vm_agent_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("claim path must not depend on agent-service readiness"),
        ),
    )

    claimed = _claim_idle_vm_inner(
        client,
        label_filter="labels.pool-role = idle",
        assistant_id="assistant-123",
        binding_id="binding-123",
        vm_type="ubuntu",
        vm_number=None,
    )

    assert claimed["vm_name"] == "unity-pool-ubuntu-2-preview"


def test_split_binding_runtime_vms_separates_current_and_other_bindings(monkeypatch):
    monkeypatch.setattr(
        vm_helpers_module,
        "list_pool_vms",
        lambda: [
            {
                "vm_name": "unity-pool-ubuntu-1-preview",
                "assistant_id": "assistant-123",
                "binding_id": "binding-current",
                "pool_role": "assigned",
            },
            {
                "vm_name": "unity-pool-ubuntu-2-preview",
                "assistant_id": "assistant-123",
                "binding_id": "binding-other",
                "pool_role": "releasing",
            },
            {
                "vm_name": "unity-pool-ubuntu-3-preview",
                "assistant_id": "assistant-999",
                "binding_id": "binding-current",
                "pool_role": "assigned",
            },
        ],
    )

    current, other = split_binding_runtime_vms(
        "assistant-123",
        binding_id="binding-current",
    )

    assert [vm["vm_name"] for vm in current] == ["unity-pool-ubuntu-1-preview"]
    assert [vm["vm_name"] for vm in other] == ["unity-pool-ubuntu-2-preview"]


def test_quarantine_pool_vm_returns_after_stop_request(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-2-preview",
        labels={"pool-role": "idle", "vm-type": "ubuntu"},
        status="RUNNING",
    )
    client = MagicMock()
    client.stop.return_value = SimpleNamespace(
        name="operation-456",
        result=lambda: (_ for _ in ()).throw(AssertionError("should not wait")),
    )

    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: True,
    )

    action = _quarantine_pool_vm(
        client,
        vm,
        reason="failed health probe during claim",
    )

    assert action == (
        "Quarantined unhealthy VM unity-pool-ubuntu-2-preview: "
        "failed health probe during claim"
    )
    client.stop.assert_called_once()


def test_release_pool_vm_transitions_to_releasing(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-preview",
        labels=_current_contract_labels(
            **{
                "pool-role": "assigned",
                "assistant-id": "assistant-123",
                "binding-id": "binding-123",
                "vm-type": "ubuntu",
            },
        ),
    )
    client = MagicMock()
    client.list.return_value = [vm]
    metadata_updates = []
    release_calls = _install_binding_lease(monkeypatch)

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._update_instance_metadata",
        lambda vm_name, updates, **_kwargs: metadata_updates.append((vm_name, updates)),
    )

    result = release_pool_vm("assistant-123", "binding-123")

    assert result["released"] is True
    assert result["pool_role"] == "releasing"
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-preview",
            {
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
            },
        ),
    ]
    assert release_calls == [True]


def test_release_pool_vm_targets_explicit_vm_name(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-preview",
        labels=_current_contract_labels(
            **{
                "pool-role": "assigned",
                "assistant-id": "assistant-123",
                "binding-id": "binding-123",
                "vm-type": "ubuntu",
            },
        ),
    )
    client = MagicMock()
    client.get.return_value = vm
    metadata_updates = []
    release_calls = _install_binding_lease(monkeypatch)

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._update_instance_metadata",
        lambda vm_name, updates, **_kwargs: metadata_updates.append((vm_name, updates)),
    )

    result = release_pool_vm(
        "assistant-123",
        "binding-123",
        vm_name="unity-pool-ubuntu-3-preview",
    )

    assert result["released"] is True
    assert result["vm_name"] == "unity-pool-ubuntu-3-preview"
    client.list.assert_not_called()
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-preview",
            {
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
            },
        ),
    ]
    assert release_calls == [True]


def test_release_pool_vm_skips_explicit_vm_when_not_owned(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-preview",
        labels=_current_contract_labels(
            **{
                "pool-role": "assigned",
                "assistant-id": "assistant-999",
                "binding-id": "binding-other",
                "vm-type": "ubuntu",
            },
        ),
    )
    client = MagicMock()
    client.get.return_value = vm
    release_calls = _install_binding_lease(monkeypatch)

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("wrongly-owned VM must not be relabeled"),
        ),
    )

    result = release_pool_vm(
        "assistant-123",
        "binding-123",
        vm_name="unity-pool-ubuntu-3-preview",
    )

    assert result["released"] is False
    assert result["message"] == "VM is not currently owned by assistant"
    assert release_calls == [True]


def test_release_pool_vm_retries_metadata_clear_while_releasing(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-preview",
        labels=_current_contract_labels(
            **{
                "pool-role": "releasing",
                "assistant-id": "assistant-123",
                "binding-id": "binding-123",
                "vm-type": "ubuntu",
            },
        ),
        metadata=SimpleNamespace(
            items=[
                SimpleNamespace(key="unify-key", value="still-set"),
            ],
        ),
    )
    client = MagicMock()
    client.list.return_value = [vm]
    metadata_updates = []
    release_calls = _install_binding_lease(monkeypatch)

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._update_instance_metadata",
        lambda vm_name, updates, **_kwargs: metadata_updates.append((vm_name, updates)),
    )

    result = release_pool_vm("assistant-123", "binding-123")

    assert result["released"] is True
    assert result["pool_role"] == "releasing"
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-preview",
            {
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
            },
        ),
    ]
    assert release_calls == [True]


def test_release_pool_vm_retires_stale_contract_vm(monkeypatch):
    stale_vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-preview",
        labels={
            "pool-role": "assigned",
            "assistant-id": "assistant-123",
            "binding-id": "binding-123",
            "vm-type": "ubuntu",
            "pool-contract-generation": "guest-contract-v1",
        },
        status="RUNNING",
    )
    client = MagicMock()
    client.list.return_value = [stale_vm]
    recycled = []
    release_calls = _install_binding_lease(monkeypatch)

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._recycle_pool_vm_instance",
        lambda *_args, **kwargs: recycled.append(kwargs["reason"]),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale-contract release must not relabel for reuse"),
        ),
    )

    result = release_pool_vm("assistant-123", "binding-123")

    assert result["released"] is True
    assert result["retired"] is True
    assert result["pool_role"] == "retired"
    assert recycled == ["assistant_release_with_stale_contract"]
    assert release_calls == [True]


def test_release_pool_vm_waits_for_binding_lease_before_releasing(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-preview",
        labels=_current_contract_labels(
            **{
                "pool-role": "assigned",
                "assistant-id": "assistant-123",
                "binding-id": "binding-123",
                "vm-type": "ubuntu",
            },
        ),
    )
    client = MagicMock()
    client.list.return_value = [vm]
    metadata_updates = []
    release_calls = _install_binding_lease(monkeypatch, acquire_results=[False, True])

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._update_instance_metadata",
        lambda vm_name, updates, **_kwargs: metadata_updates.append((vm_name, updates)),
    )

    result = release_pool_vm("assistant-123", "binding-123")

    assert result["released"] is True
    assert result["pool_role"] == "releasing"
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-preview",
            {
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
            },
        ),
    ]
    assert release_calls == [True]


def test_release_pool_vm_skips_when_binding_lease_stays_busy(monkeypatch):
    release_calls = _install_binding_lease(monkeypatch, acquire_results=[False])

    monkeypatch.setattr(
        vm_helpers_module,
        "VM_BINDING_RELEASE_LEASE_WAIT_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: (_ for _ in ()).throw(
            AssertionError("busy release must not touch the compute API"),
        ),
    )

    result = release_pool_vm("assistant-123", "binding-123")

    assert result == {
        "released": False,
        "assistant_id": "assistant-123",
        "binding_id": "binding-123",
        "vm_name": None,
        "reason": "binding_operation_busy",
        "message": "Another binding VM operation is still in progress",
    }
    assert release_calls == []


def test_complete_pool_vm_release_detaches_disk_and_marks_idle(monkeypatch):
    releasing_vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-preview",
        status="RUNNING",
        labels=_current_contract_labels(
            **{
                "pool-role": "releasing",
                "assistant-id": "assistant-123",
                "binding-id": "binding-123",
                "vm-type": "ubuntu",
            },
        ),
    )
    client = MagicMock()
    client.get.return_value = releasing_vm
    metadata_updates = []

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._detach_attached_assistant_disk",
        lambda vm_name: (True, "unity-disk-assistant-123"),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._update_instance_metadata",
        lambda vm_name, updates, **_kwargs: metadata_updates.append((vm_name, updates)),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: True,
    )

    result = complete_pool_vm_release("unity-pool-ubuntu-3-preview", "binding-123")

    assert result["pool_role"] == "idle"
    assert result["detached"] is True
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-preview",
            {
                "assistant-id": "",
                "binding-id": "",
                "disk-device": "",
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
            },
        ),
    ]


def test_complete_pool_vm_release_is_idempotent_once_vm_is_idle(monkeypatch):
    idle_vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-preview",
        status="RUNNING",
        labels=_current_contract_labels(
            **{
                "pool-role": "idle",
                "assistant-id": "",
                "binding-id": "",
                "vm-type": "ubuntu",
            },
        ),
    )
    client = MagicMock()
    client.get.return_value = idle_vm

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._detach_attached_assistant_disk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("already idle vm must not detach disks"),
        ),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._update_instance_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("already idle vm must not rewrite metadata"),
        ),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("already idle vm must not rewrite labels"),
        ),
    )

    result = complete_pool_vm_release("unity-pool-ubuntu-3-preview", "binding-123")

    assert result == {
        "vm_name": "unity-pool-ubuntu-3-preview",
        "vm_type": "ubuntu",
        "pool_role": "idle",
        "assistant_id": None,
        "binding_id": None,
        "already_released": True,
    }


def test_assign_pool_vm_finalizes_stale_releasing_disk_owner_before_claim(monkeypatch):
    claim_idle = MagicMock(
        return_value={
            "vm_name": "unity-pool-ubuntu-4-preview",
            "ip_address": "34.0.0.4",
            "hostname": "unity-pool-ubuntu-4-preview.vm.unify.ai",
            "desktop_url": "https://unity-pool-ubuntu-4-preview.vm.unify.ai",
            "status": "RUNNING",
        },
    )
    complete_release = MagicMock(
        return_value={"vm_name": "unity-pool-ubuntu-3-preview", "pool_role": "idle"},
    )
    find_disk_owner = MagicMock(
        side_effect=["unity-pool-ubuntu-3-preview", None],
    )

    monkeypatch.setattr(
        "communication.infra.helpers.setup_kubernetes_client",
        lambda: (None, None, None, object()),
    )
    monkeypatch.setattr(
        "communication.infra.helpers.acquire_assignment_lease",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "communication.infra.helpers.release_assignment_lease",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.find_vm_with_disk",
        find_disk_owner,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._attached_disk_vm_state",
        lambda *_args, **_kwargs: {
            "vm_name": "unity-pool-ubuntu-3-preview",
            "binding_id": "binding-old",
            "pool_role": "releasing",
        },
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.complete_pool_vm_release",
        complete_release,
    )
    monkeypatch.setattr("communication.infra.vm_helpers.claim_idle_vm", claim_idle)
    monkeypatch.setattr(
        "communication.infra.vm_helpers.create_assistant_disk",
        lambda *_args, **_kwargs: "disk-self-link",
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.attach_assistant_disk",
        lambda *_args, **_kwargs: "unity-disk-assistant-123",
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._fetch_existing_ssh_key",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.generate_ssh_keypair",
        lambda: ("PRIVATE", "PUBLIC"),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.store_ssh_private_key",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.get_secret",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._update_instance_metadata",
        lambda *_args, **_kwargs: None,
    )

    result = assign_pool_vm("assistant-123", "binding-new", "unify-key")

    complete_release.assert_called_once_with(
        "unity-pool-ubuntu-3-preview",
        "binding-old",
    )
    claim_idle.assert_called_once_with(
        "assistant-123",
        "binding-new",
        "ubuntu",
        vm_number=None,
    )
    assert result["vm_name"] == "unity-pool-ubuntu-4-preview"


def test_assign_pool_vm_raises_when_disk_owned_by_active_other_binding(monkeypatch):
    claim_idle = MagicMock()

    monkeypatch.setattr(
        "communication.infra.helpers.setup_kubernetes_client",
        lambda: (None, None, None, object()),
    )
    monkeypatch.setattr(
        "communication.infra.helpers.acquire_assignment_lease",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "communication.infra.helpers.release_assignment_lease",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.find_vm_with_disk",
        lambda *_args, **_kwargs: "unity-pool-ubuntu-3-preview",
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._attached_disk_vm_state",
        lambda *_args, **_kwargs: {
            "vm_name": "unity-pool-ubuntu-3-preview",
            "binding_id": "binding-old",
            "pool_role": "assigned",
        },
    )
    monkeypatch.setattr("communication.infra.vm_helpers.claim_idle_vm", claim_idle)

    with pytest.raises(AssistantDiskInUseError, match="binding-old"):
        assign_pool_vm("assistant-123", "binding-new", "unify-key")

    claim_idle.assert_not_called()


def test_delete_assistant_disk_raises_when_disk_still_attached(monkeypatch):
    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.DisksClient",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.find_vm_with_disk",
        lambda *_args, **_kwargs: "unity-pool-ubuntu-3-preview",
    )

    with pytest.raises(AssistantDiskInUseError):
        delete_assistant_disk("assistant-123")


def test_scrub_inconsistent_vms_submits_stop_without_waiting(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-15-preview",
        labels={"pool-role": "quarantined", "vm-type": "ubuntu"},
        status="RUNNING",
    )
    client = MagicMock()
    client.list.return_value = [vm]
    client.stop.return_value = SimpleNamespace(
        name="operation-789",
        result=lambda: (_ for _ in ()).throw(AssertionError("should not wait")),
    )

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )

    actions = vm_helpers_module._scrub_inconsistent_vms("ubuntu")

    assert actions == [
        "Scrub: stop requested for unity-pool-ubuntu-15-preview "
        "(quarantined_but_running)",
    ]
    client.stop.assert_called_once()


def test_replenish_pool_hot_path_skips_bulk_idle_health_sweep(monkeypatch):
    stopped_vm = SimpleNamespace(
        name="unity-pool-ubuntu-6-preview",
        labels={"vm-type": "ubuntu"},
    )

    monkeypatch.setattr(vm_helpers_module, "POOL_TARGET_IDLE", 1)
    monkeypatch.setattr(vm_helpers_module, "POOL_TARGET_STOPPED", 1)
    monkeypatch.setattr(
        vm_helpers_module,
        "_recycle_stale_pool_vms",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_quarantine_stale_inflight_vms",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_scrub_inconsistent_vms",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_probe_and_quarantine_unhealthy_idle_vms",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("bulk idle sweep should not run in replenish"),
        ),
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_list_pool_state",
        lambda *_args, **_kwargs: (
            MagicMock(),
            [],
            [],
            [stopped_vm],
            [],
            {stopped_vm.name},
        ),
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_start_one_stopped_vm",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "provision_pool_vm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("replenish should use the stopped reserve first"),
        ),
    )

    result = replenish_pool("ubuntu")

    assert result["vm_type"] == "ubuntu"
    assert result["actions"] == ["Started stopped VM unity-pool-ubuntu-6-preview"]


def test_trim_stopped_pool_reserve_deletes_oldest_excess_vms(monkeypatch):
    client = MagicMock()
    client.delete.return_value = SimpleNamespace(result=lambda: None)
    stopped_vms = [
        SimpleNamespace(
            name="unity-pool-ubuntu-14-preview",
            labels=_current_contract_labels(
                **{"pool-role": "stopped", "vm-type": "ubuntu"},
            ),
            status="TERMINATED",
            last_stop_timestamp="2026-04-06T09:00:00+00:00",
        ),
        SimpleNamespace(
            name="unity-pool-ubuntu-15-preview",
            labels=_current_contract_labels(
                **{"pool-role": "stopped", "vm-type": "ubuntu"},
            ),
            status="TERMINATED",
            last_stop_timestamp="2026-04-06T08:59:00+00:00",
        ),
        SimpleNamespace(
            name="unity-pool-ubuntu-16-preview",
            labels=_current_contract_labels(
                **{"pool-role": "stopped", "vm-type": "ubuntu"},
            ),
            status="TERMINATED",
            last_stop_timestamp="2026-04-06T08:58:00+00:00",
        ),
        SimpleNamespace(
            name="unity-pool-ubuntu-17-preview",
            labels=_current_contract_labels(
                **{"pool-role": "stopped", "vm-type": "ubuntu"},
            ),
            status="TERMINATED",
            last_stop_timestamp="2026-04-06T08:57:00+00:00",
        ),
    ]

    monkeypatch.setattr(vm_helpers_module, "POOL_TARGET_STOPPED", 2)
    monkeypatch.setattr(
        vm_helpers_module,
        "_list_pool_state",
        lambda *_args, **_kwargs: (client, [], [], stopped_vms, [], set()),
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_log_vm_pool_event",
        lambda *_args, **_kwargs: None,
    )

    result = vm_helpers_module.trim_stopped_pool_reserve("ubuntu")

    assert result["kept"] == [
        "unity-pool-ubuntu-14-preview",
        "unity-pool-ubuntu-15-preview",
    ]
    assert result["deleted"] == [
        "unity-pool-ubuntu-16-preview",
        "unity-pool-ubuntu-17-preview",
    ]
    assert result["actions"] == [
        "Deleted excess stopped reserve VM unity-pool-ubuntu-16-preview",
        "Deleted excess stopped reserve VM unity-pool-ubuntu-17-preview",
    ]
    assert result["errors"] == []
    assert [call.kwargs["instance"] for call in client.delete.call_args_list] == [
        "unity-pool-ubuntu-16-preview",
        "unity-pool-ubuntu-17-preview",
    ]


def test_rebalance_pool_includes_stopped_reserve_prune_actions(monkeypatch):
    monkeypatch.setattr(
        vm_helpers_module,
        "_scrub_inconsistent_vms",
        lambda *_args, **_kwargs: ["scrubbed ghost"],
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "replenish_pool",
        lambda *_args, **_kwargs: {"actions": ["replenished idle"]},
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "trim_pool",
        lambda *_args, **_kwargs: {"actions": ["trimmed idle"]},
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "trim_stopped_pool_reserve",
        lambda *_args, **_kwargs: {
            "actions": ["deleted old reserve"],
            "deleted": ["unity-pool-ubuntu-17-preview"],
            "kept": ["unity-pool-ubuntu-14-preview"],
        },
    )

    result = vm_helpers_module.rebalance_pool("ubuntu")

    assert result == {
        "vm_type": "ubuntu",
        "actions": [
            "scrubbed ghost",
            "replenished idle",
            "trimmed idle",
            "deleted old reserve",
        ],
        "stopped_reserve_deleted": ["unity-pool-ubuntu-17-preview"],
        "stopped_reserve_kept": ["unity-pool-ubuntu-14-preview"],
    }
