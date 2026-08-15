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
    delete_assistant_pool_archive,
    reclaim_orphaned_assistant_disk,
    release_pool_vm,
    replenish_pool,
    retire_pool_vm_release,
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
        name="unity-pool-ubuntu-6-staging",
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
        name="unity-pool-ubuntu-6-staging",
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
            "unity-pool-ubuntu-6-staging",
            {
                "pool-watcher-script": "watcher",
                "pool-contract-generation": vm_helpers_module.POOL_VM_CONTRACT_GENERATION,
            },
        ),
    ]


def test_claim_idle_vm_does_not_require_agent_service_before_assignment(monkeypatch):
    pool_vm = SimpleNamespace(
        name="unity-pool-ubuntu-2-staging",
        labels=_current_contract_labels(**{"pool-role": "idle", "vm-type": "ubuntu"}),
        label_fingerprint="unity-pool-ubuntu-2-staging-fp",
        network_interfaces=[],
        metadata=SimpleNamespace(
            items=[
                SimpleNamespace(
                    key="hostname",
                    value="unity-pool-ubuntu-2-staging.example.com",
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

    assert claimed["vm_name"] == "unity-pool-ubuntu-2-staging"


def test_split_binding_runtime_vms_separates_current_and_other_bindings(monkeypatch):
    monkeypatch.setattr(
        vm_helpers_module,
        "list_pool_vms",
        lambda: [
            {
                "vm_name": "unity-pool-ubuntu-1-staging",
                "assistant_id": "assistant-123",
                "binding_id": "binding-current",
                "pool_role": "assigned",
            },
            {
                "vm_name": "unity-pool-ubuntu-2-staging",
                "assistant_id": "assistant-123",
                "binding_id": "binding-other",
                "pool_role": "releasing",
            },
            {
                "vm_name": "unity-pool-ubuntu-3-staging",
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

    assert [vm["vm_name"] for vm in current] == ["unity-pool-ubuntu-1-staging"]
    assert [vm["vm_name"] for vm in other] == ["unity-pool-ubuntu-2-staging"]


def test_quarantine_pool_vm_returns_after_stop_request(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-2-staging",
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
        "Quarantined unhealthy VM unity-pool-ubuntu-2-staging: "
        "failed health probe during claim"
    )
    client.stop.assert_called_once()


def test_release_pool_vm_transitions_to_releasing(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
        labels=_current_contract_labels(
            **{
                "pool-role": "assigned",
                "assistant-id": "assistant-123",
                "binding-id": "binding-123",
                "vm-type": "ubuntu",
            },
        ),
        metadata=SimpleNamespace(items=[]),
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
    assert result["release_generation"] == 1
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-staging",
            {
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
                vm_helpers_module.RELEASE_GENERATION_METADATA_KEY: "1",
            },
        ),
    ]
    assert release_calls == [True]


def test_release_pool_vm_targets_explicit_vm_name(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
        labels=_current_contract_labels(
            **{
                "pool-role": "assigned",
                "assistant-id": "assistant-123",
                "binding-id": "binding-123",
                "vm-type": "ubuntu",
            },
        ),
        metadata=SimpleNamespace(items=[]),
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
        vm_name="unity-pool-ubuntu-3-staging",
    )

    assert result["released"] is True
    assert result["vm_name"] == "unity-pool-ubuntu-3-staging"
    assert result["release_generation"] == 1
    client.list.assert_not_called()
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-staging",
            {
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
                vm_helpers_module.RELEASE_GENERATION_METADATA_KEY: "1",
            },
        ),
    ]
    assert release_calls == [True]


def test_release_pool_vm_skips_explicit_vm_when_not_owned(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
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
        vm_name="unity-pool-ubuntu-3-staging",
    )

    assert result["released"] is False
    assert result["message"] == "VM is not currently owned by assistant"
    assert release_calls == [True]


def test_release_pool_vm_retries_metadata_clear_while_releasing(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
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
    assert result["release_generation"] == 1
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-staging",
            {
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
                vm_helpers_module.RELEASE_GENERATION_METADATA_KEY: "1",
            },
        ),
    ]
    assert release_calls == [True]


def test_release_pool_vm_rearms_with_new_release_generation(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
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
                SimpleNamespace(
                    key=vm_helpers_module.RELEASE_GENERATION_METADATA_KEY,
                    value="1",
                ),
            ],
        ),
    )
    client = MagicMock()
    client.list.return_value = [vm]
    metadata_updates = []
    release_calls = _install_binding_lease(monkeypatch)
    set_pool_labels = MagicMock(return_value=True)

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        set_pool_labels,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._update_instance_metadata",
        lambda vm_name, updates, **_kwargs: metadata_updates.append((vm_name, updates)),
    )

    result = release_pool_vm(
        "assistant-123",
        "binding-123",
        release_generation=2,
    )

    assert result["released"] is True
    assert result["rearmed"] is True
    assert result["release_generation"] == 2
    set_pool_labels.assert_called_once_with(
        client,
        "unity-pool-ubuntu-3-staging",
        {"pool-role": "releasing"},
        expected_role="releasing",
    )
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-staging",
            {
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
                vm_helpers_module.RELEASE_GENERATION_METADATA_KEY: "2",
            },
        ),
    ]
    assert release_calls == [True]


def test_release_pool_vm_retires_stale_contract_vm(monkeypatch):
    stale_vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
        labels={
            "pool-role": "assigned",
            "assistant-id": "assistant-123",
            "binding-id": "binding-123",
            "vm-type": "ubuntu",
            "pool-contract-generation": "guest-contract-v1",
        },
        status="RUNNING",
        metadata=SimpleNamespace(items=[]),
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
    assert result["release_generation"] == 1
    assert recycled == ["assistant_release_with_stale_contract"]
    assert release_calls == [True]


def test_retire_pool_vm_release_retires_quarantined_vm(monkeypatch):
    """The scrubber reaching a stuck VM first must not strand the session.

    Quarantining sets pool-role=quarantined and clears assistant-id, keeping
    binding-id. Retire is the session's last-resort recovery for exactly that
    VM, so the binding is what proves ownership.
    """
    quarantined_vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
        labels=_current_contract_labels(
            **{
                "pool-role": "quarantined",
                "assistant-id": "",
                "binding-id": "binding-123",
                "vm-type": "ubuntu",
            },
        ),
        status="RUNNING",
        metadata=SimpleNamespace(items=[]),
    )
    client = MagicMock()
    client.get.return_value = quarantined_vm
    recycled = []
    replenished = []
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
        "communication.infra.vm_helpers.replenish_pool",
        lambda vm_type: replenished.append(vm_type),
    )

    result = retire_pool_vm_release(
        "assistant-123",
        "binding-123",
        vm_name="unity-pool-ubuntu-3-staging",
        reason="controller_desired_stop_release_timeout",
    )

    assert result["retired"] is True
    assert result["released"] is True
    assert result["pool_role"] == "retired"
    assert recycled == ["controller_desired_stop_release_timeout"]
    assert replenished == ["ubuntu"]
    assert release_calls == [True]


def test_retire_pool_vm_release_skips_vm_held_by_another_binding(monkeypatch):
    """A newer assignment must survive a stale release's retire attempt."""
    reassigned_vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
        labels=_current_contract_labels(
            **{
                "pool-role": "assigned",
                "assistant-id": "assistant-999",
                "binding-id": "binding-999",
                "vm-type": "ubuntu",
            },
        ),
        status="RUNNING",
        metadata=SimpleNamespace(items=[]),
    )
    client = MagicMock()
    client.get.return_value = reassigned_vm
    _install_binding_lease(monkeypatch)

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._recycle_pool_vm_instance",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not retire a VM held by another binding"),
        ),
    )

    result = retire_pool_vm_release(
        "assistant-123",
        "binding-123",
        vm_name="unity-pool-ubuntu-3-staging",
        reason="controller_desired_stop_release_timeout",
    )

    assert result["skipped"] is True
    assert result["reason"] == "vm_not_owned"
    assert result.get("retired") is None


def test_recycle_pool_vm_restores_assistant_ip_before_deleting(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
        labels={
            "vm-type": "ubuntu",
            "assistant-id": "assistant-123",
        },
    )
    actions = []
    monkeypatch.setattr(
        vm_helpers_module,
        "restore_pool_static_ip_on_vm",
        lambda vm_name, assistant_id, vm_type: actions.append(
            ("restore", vm_name, assistant_id, vm_type),
        ),
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_delete_pool_vm_instance",
        lambda _client, vm_name, **_kwargs: actions.append(("delete", vm_name)),
    )

    vm_helpers_module._recycle_pool_vm_instance(
        MagicMock(),
        vm,
        reason="test",
    )

    assert actions == [
        ("restore", "unity-pool-ubuntu-3-staging", "assistant-123", "ubuntu"),
        ("delete", "unity-pool-ubuntu-3-staging"),
    ]


def test_release_pool_vm_waits_for_binding_lease_before_releasing(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
        labels=_current_contract_labels(
            **{
                "pool-role": "assigned",
                "assistant-id": "assistant-123",
                "binding-id": "binding-123",
                "vm-type": "ubuntu",
            },
        ),
        metadata=SimpleNamespace(items=[]),
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
    assert result["release_generation"] == 1
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-staging",
            {
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
                vm_helpers_module.RELEASE_GENERATION_METADATA_KEY: "1",
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


def test_reconcile_orphaned_vms_recovers_aged_releasing_vm(monkeypatch):
    releasing_vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
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
                SimpleNamespace(
                    key=vm_helpers_module.RELEASE_GENERATION_METADATA_KEY,
                    value="1",
                ),
            ],
        ),
        status="RUNNING",
        last_start_timestamp=(datetime.now(UTC) - timedelta(seconds=1200)).isoformat(),
        creation_timestamp=(datetime.now(UTC) - timedelta(seconds=1200)).isoformat(),
    )
    client = MagicMock()
    client.list.side_effect = [[], [releasing_vm]]
    client.get.return_value = releasing_vm
    recover_release = MagicMock(
        return_value={
            "action": "rearmed",
            "release_generation": 2,
            "retired": False,
        },
    )

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._refresh_inflight_progress_phase",
        lambda *_args, **_kwargs: releasing_vm,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.recover_stuck_pool_vm_release",
        recover_release,
    )

    result = vm_helpers_module.reconcile_orphaned_vms(MagicMock(), "ubuntu")

    recover_release.assert_called_once_with(
        "assistant-123",
        "binding-123",
        vm_name="unity-pool-ubuntu-3-staging",
        current_release_generation=1,
        allow_rearm=True,
        retire_reason="aged_releasing_vm",
    )
    assert result["releasing_checked"] == 1
    assert result["releasing_recovered"] == [
        {
            "vm_name": "unity-pool-ubuntu-3-staging",
            "assistant_id": "assistant-123",
            "binding_id": "binding-123",
            "action": "rearmed",
            "release_generation": 2,
            "retired": False,
        },
    ]


def test_complete_pool_vm_release_detaches_disk_and_marks_idle(monkeypatch):
    releasing_vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
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
        "communication.infra.vm_helpers.restore_pool_static_ip_on_vm",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._update_instance_metadata",
        lambda vm_name, updates, **_kwargs: metadata_updates.append((vm_name, updates)),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: True,
    )

    result = complete_pool_vm_release("unity-pool-ubuntu-3-staging", "binding-123")

    assert result["pool_role"] == "idle"
    assert result["detached"] is True
    assert metadata_updates == [
        (
            "unity-pool-ubuntu-3-staging",
            {
                "assistant-id": "",
                "binding-id": "",
                "disk-device": "",
                "unify-key": "",
                "vnc-password": "",
                "ssh-public-key": "",
                vm_helpers_module.RELEASE_GENERATION_METADATA_KEY: "",
                "hostname": "unity-pool-ubuntu-3-staging.vm.unify.ai",
            },
        ),
    ]


def test_complete_pool_vm_release_is_idempotent_once_vm_is_idle(monkeypatch):
    idle_vm = SimpleNamespace(
        name="unity-pool-ubuntu-3-staging",
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

    result = complete_pool_vm_release("unity-pool-ubuntu-3-staging", "binding-123")

    assert result == {
        "vm_name": "unity-pool-ubuntu-3-staging",
        "vm_type": "ubuntu",
        "pool_role": "idle",
        "assistant_id": None,
        "binding_id": None,
        "already_released": True,
    }


def test_assign_pool_vm_finalizes_stale_releasing_disk_owner_before_claim(monkeypatch):
    claim_idle = MagicMock(
        return_value={
            "vm_name": "unity-pool-ubuntu-4-staging",
            "ip_address": "34.0.0.4",
            "hostname": "unity-pool-ubuntu-4-staging.vm.unify.ai",
            "desktop_url": "https://unity-pool-ubuntu-4-staging.vm.unify.ai",
            "status": "RUNNING",
        },
    )
    reclaim_disk = MagicMock(
        return_value={
            "vm_name": "unity-pool-ubuntu-3-staging",
            "detached": True,
        },
    )
    find_disk_owner = MagicMock(
        side_effect=["unity-pool-ubuntu-3-staging", None],
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
            "vm_name": "unity-pool-ubuntu-3-staging",
            "binding_id": "binding-old",
            "pool_role": "releasing",
        },
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.reclaim_orphaned_assistant_disk",
        reclaim_disk,
    )
    monkeypatch.setattr("communication.infra.vm_helpers.claim_idle_vm", claim_idle)
    monkeypatch.setattr(
        "communication.infra.vm_helpers.attach_assistant_static_ip_to_pool_vm",
        lambda *_args, **_kwargs: {
            "hostname": "unity-assistant-assistant-123-staging.vm.unify.ai",
            "ip_address": "34.0.0.123",
        },
    )
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

    reclaim_disk.assert_called_once_with(
        "assistant-123",
        current_binding_id="binding-new",
    )
    claim_idle.assert_called_once_with(
        "assistant-123",
        "binding-new",
        "ubuntu",
        vm_number=None,
    )
    assert result["vm_name"] == "unity-pool-ubuntu-4-staging"
    assert result["hostname"] == "unity-assistant-assistant-123-staging.vm.unify.ai"
    assert result["ip_address"] == "34.0.0.123"


def test_assign_pool_vm_mints_distinct_desktop_secret(monkeypatch):
    update_metadata = MagicMock()

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
        "communication.infra.vm_helpers._ensure_disk_ready_for_binding",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.reclaim_stale_assistant_ip_owners",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.claim_idle_vm",
        lambda *_args, **_kwargs: {
            "vm_name": "unity-pool-ubuntu-4-staging",
            "ip_address": "34.0.0.4",
            "hostname": "unity-pool-ubuntu-4-staging.vm.unify.ai",
            "desktop_url": "https://unity-pool-ubuntu-4-staging.vm.unify.ai",
            "status": "RUNNING",
        },
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.attach_assistant_static_ip_to_pool_vm",
        lambda *_args, **_kwargs: {
            "hostname": "unity-assistant-assistant-123-staging.vm.unify.ai",
            "ip_address": "34.0.0.123",
        },
    )
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
        update_metadata,
    )

    result = assign_pool_vm("assistant-123", "binding-new", "unify-key-abc")

    metadata = update_metadata.call_args.args[1]
    assert metadata["unify-key"] == "unify-key-abc"
    assert metadata["vnc-password"] != "unify-key-abc"
    assert len(metadata["vnc-password"]) >= 16
    assert result["desktop_secret"] == metadata["vnc-password"]


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
        lambda *_args, **_kwargs: "unity-pool-ubuntu-3-staging",
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._attached_disk_vm_state",
        lambda *_args, **_kwargs: {
            "vm_name": "unity-pool-ubuntu-3-staging",
            "binding_id": "binding-old",
            "pool_role": "assigned",
        },
    )
    monkeypatch.setattr("communication.infra.vm_helpers.claim_idle_vm", claim_idle)

    with pytest.raises(AssistantDiskInUseError, match="binding-old"):
        assign_pool_vm("assistant-123", "binding-new", "unify-key")

    claim_idle.assert_not_called()


def _adopt_patches(monkeypatch, *, owner_binding_id: str, pool_role: str):
    """Present a disk already attached to a VM labelled for ``owner_binding_id``."""
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
        lambda *_args, **_kwargs: "unity-pool-ubuntu-1-staging",
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._attached_disk_vm_state",
        lambda *_args, **_kwargs: {
            "vm_name": "unity-pool-ubuntu-1-staging",
            "assistant_id": "assistant-123",
            "binding_id": owner_binding_id,
            "pool_role": pool_role,
            "hostname": "unity-pool-ubuntu-1-staging.vm.unify.ai",
            "ip_address": "34.0.0.1",
        },
    )


def test_assign_pool_vm_adopts_the_vm_its_own_binding_already_owns(monkeypatch):
    """A torn assignment resumes instead of deadlocking on its own disk.

    When a previous attempt claimed the VM, attached the disk and labelled both
    for this binding but never persisted the vmRef, the disk guard used to raise
    forever: the reclaim path deliberately skips VMs that are ``assigned``, and
    the orphan sweeper only releases VMs whose binding has no live Job.
    """
    claim_idle = MagicMock()
    _adopt_patches(monkeypatch, owner_binding_id="binding-1", pool_role="assigned")
    monkeypatch.setattr("communication.infra.vm_helpers.claim_idle_vm", claim_idle)
    monkeypatch.setattr(
        "communication.infra.vm_helpers.attach_assistant_static_ip_to_pool_vm",
        lambda *_args, **_kwargs: {
            "hostname": "unity-assistant-123-staging.vm.unify.ai",
            "ip_address": "34.0.0.9",
        },
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        MagicMock(),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._read_instance_metadata",
        lambda _instance, key: "recovered-secret" if key == "vnc-password" else None,
    )

    result = assign_pool_vm("assistant-123", "binding-1", "unify-key")

    claim_idle.assert_not_called()
    assert result["vm_name"] == "unity-pool-ubuntu-1-staging"
    assert result["binding_id"] == "binding-1"
    # Read back, not re-minted: the guest already configured VNC with this value
    # and rewriting metadata would not re-trigger the pool watcher.
    assert result["desktop_secret"] == "recovered-secret"
    # Static IP attach is re-run, so a torn attempt that died before that stage
    # still converges on the assistant hostname rather than the pool one.
    assert result["hostname"] == "unity-assistant-123-staging.vm.unify.ai"
    assert result["desktop_url"] == "https://unity-assistant-123-staging.vm.unify.ai"


def test_assign_pool_vm_waits_rather_than_adopting_a_releasing_own_vm(monkeypatch):
    """A VM mid-release is not adoptable even for the same binding.

    Release teardown archives and detaches; adopting through it would race the
    guest. Raising surfaces as ``waiting_release`` so the assignment retries
    once the release completes.
    """
    claim_idle = MagicMock()
    _adopt_patches(monkeypatch, owner_binding_id="binding-1", pool_role="releasing")
    monkeypatch.setattr("communication.infra.vm_helpers.claim_idle_vm", claim_idle)

    with pytest.raises(AssistantDiskInUseError, match="releasing"):
        assign_pool_vm("assistant-123", "binding-1", "unify-key")

    claim_idle.assert_not_called()


def test_reclaim_orphaned_disk_detaches_from_idle_vm(monkeypatch):
    detach = MagicMock(return_value=True)
    monkeypatch.setattr(
        "communication.infra.vm_helpers.find_vm_with_disk",
        lambda *_args, **_kwargs: "unity-pool-ubuntu-1-staging",
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._attached_disk_vm_state",
        lambda *_args, **_kwargs: {
            "vm_name": "unity-pool-ubuntu-1-staging",
            "assistant_id": "",
            "binding_id": "",
            "pool_role": "idle",
        },
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.detach_assistant_disk",
        detach,
    )

    result = reclaim_orphaned_assistant_disk("2101", current_binding_id="bc0d")

    assert result["detached"] is True
    assert result["vm_name"] == "unity-pool-ubuntu-1-staging"
    detach.assert_called_once_with("unity-pool-ubuntu-1-staging", "2101")


def test_reclaim_orphaned_disk_skips_assigned_vm(monkeypatch):
    detach = MagicMock()
    monkeypatch.setattr(
        "communication.infra.vm_helpers.find_vm_with_disk",
        lambda *_args, **_kwargs: "unity-pool-ubuntu-1-staging",
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._attached_disk_vm_state",
        lambda *_args, **_kwargs: {
            "vm_name": "unity-pool-ubuntu-1-staging",
            "assistant_id": "2101",
            "binding_id": "bc0d",
            "pool_role": "assigned",
        },
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.detach_assistant_disk",
        detach,
    )

    result = reclaim_orphaned_assistant_disk("2101", current_binding_id="bc0d")

    assert result["detached"] is False
    assert result["reason"] == "vm_assigned"
    detach.assert_not_called()


def test_reclaim_orphaned_disk_noop_when_no_disk(monkeypatch):
    monkeypatch.setattr(
        "communication.infra.vm_helpers.find_vm_with_disk",
        lambda *_args, **_kwargs: None,
    )

    result = reclaim_orphaned_assistant_disk("2101")

    assert result == {"detached": False, "reason": "no_attached_disk"}


def test_delete_assistant_disk_raises_when_disk_still_attached(monkeypatch):
    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.DisksClient",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.find_vm_with_disk",
        lambda *_args, **_kwargs: "unity-pool-ubuntu-3-staging",
    )

    with pytest.raises(AssistantDiskInUseError):
        delete_assistant_disk("assistant-123")


def test_delete_assistant_pool_archive_removes_local_and_profile_blobs(monkeypatch):
    deleted: list[str] = []

    class FakeBlob:
        def __init__(self, name: str, exists: bool):
            self.name = name
            self._exists = exists

        def exists(self):
            return self._exists

        def delete(self):
            deleted.append(self.name)

    class FakeBucket:
        def blob(self, name: str):
            # Local archive present; profile present
            return FakeBlob(name, exists=True)

    class FakeClient:
        def bucket(self, _name: str):
            return FakeBucket()

    monkeypatch.setattr(
        "google.cloud.storage.Client",
        lambda *args, **kwargs: FakeClient(),
    )
    monkeypatch.delenv("GCP_SA_KEY", raising=False)

    result = delete_assistant_pool_archive("asst-42")

    assert result["assistant_id"] == "asst-42"
    assert deleted == [
        "asst-42.tar.gz",
        "asst-42-desktop-profile.tar.gz",
    ]
    assert result["deleted"] == deleted
    assert result["missing"] == []


def test_scrub_inconsistent_vms_submits_stop_without_waiting(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-15-staging",
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
        "Scrub: stop requested for unity-pool-ubuntu-15-staging "
        "(quarantined_but_running)",
    ]
    client.stop.assert_called_once()


def test_replenish_pool_hot_path_skips_bulk_idle_health_sweep(monkeypatch):
    stopped_vm = SimpleNamespace(
        name="unity-pool-ubuntu-6-staging",
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
    assert result["actions"] == ["Started stopped VM unity-pool-ubuntu-6-staging"]


def test_trim_pool_keeps_fresh_idle_vms_during_grace(monkeypatch):
    """Cold-start idle VMs must stay claimable until the grace window elapses."""

    now = datetime.now(UTC)
    fresh_idle = SimpleNamespace(
        name="unity-pool-ubuntu-2-staging",
        labels=_current_contract_labels(
            **{
                "pool-role": "idle",
                "vm-type": "ubuntu",
                "pool-progress-phase": "idle",
                "pool-progress-epoch": str(int(now.timestamp()) - 30),
            },
        ),
        status="RUNNING",
        last_start_timestamp=(now - timedelta(seconds=30)).isoformat(),
    )
    client = MagicMock()
    set_labels = MagicMock(return_value=True)

    monkeypatch.setattr(vm_helpers_module, "POOL_TARGET_IDLE", 0)
    monkeypatch.setattr(vm_helpers_module, "POOL_IDLE_TRIM_GRACE_SECONDS", 300.0)
    monkeypatch.setattr(
        vm_helpers_module,
        "_list_pool_state",
        lambda *_args, **_kwargs: (client, [], [fresh_idle], [], [], {fresh_idle.name}),
    )
    monkeypatch.setattr(vm_helpers_module, "_set_pool_labels", set_labels)
    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )

    result = vm_helpers_module.trim_pool("ubuntu")

    assert result["actions"] == []
    set_labels.assert_not_called()
    client.stop.assert_not_called()


def test_trim_pool_respects_pending_claims_target(monkeypatch):
    """In-process pending claims raise the idle floor above POOL_TARGET_IDLE."""

    now = datetime.now(UTC)
    old_idle = SimpleNamespace(
        name="unity-pool-ubuntu-2-staging",
        labels=_current_contract_labels(
            **{
                "pool-role": "idle",
                "vm-type": "ubuntu",
                "pool-progress-phase": "idle",
                "pool-progress-epoch": str(int(now.timestamp()) - 900),
            },
        ),
        status="RUNNING",
        last_start_timestamp=(now - timedelta(seconds=900)).isoformat(),
    )
    client = MagicMock()
    set_labels = MagicMock(return_value=True)

    monkeypatch.setattr(vm_helpers_module, "POOL_TARGET_IDLE", 0)
    monkeypatch.setattr(vm_helpers_module, "POOL_IDLE_TRIM_GRACE_SECONDS", 300.0)
    monkeypatch.setattr(
        vm_helpers_module,
        "_list_pool_state",
        lambda *_args, **_kwargs: (client, [], [old_idle], [], [], {old_idle.name}),
    )
    monkeypatch.setattr(vm_helpers_module, "_set_pool_labels", set_labels)
    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    pending_key = vm_helpers_module._pool_scope_key("ubuntu")
    with vm_helpers_module._pending_lock:
        vm_helpers_module._pending_claims[pending_key] = 1

    try:
        result = vm_helpers_module.trim_pool("ubuntu")
    finally:
        with vm_helpers_module._pending_lock:
            vm_helpers_module._pending_claims.pop(pending_key, None)

    assert result["actions"] == []
    set_labels.assert_not_called()
    client.stop.assert_not_called()


def test_trim_stopped_pool_reserve_deletes_oldest_excess_vms(monkeypatch):
    client = MagicMock()
    deleted_vm_names = []
    stopped_vms = [
        SimpleNamespace(
            name="unity-pool-ubuntu-14-staging",
            labels=_current_contract_labels(
                **{"pool-role": "stopped", "vm-type": "ubuntu"},
            ),
            status="TERMINATED",
            last_stop_timestamp="2026-04-06T09:00:00+00:00",
        ),
        SimpleNamespace(
            name="unity-pool-ubuntu-15-staging",
            labels=_current_contract_labels(
                **{"pool-role": "stopped", "vm-type": "ubuntu"},
            ),
            status="TERMINATED",
            last_stop_timestamp="2026-04-06T08:59:00+00:00",
        ),
        SimpleNamespace(
            name="unity-pool-ubuntu-16-staging",
            labels=_current_contract_labels(
                **{"pool-role": "stopped", "vm-type": "ubuntu"},
            ),
            status="TERMINATED",
            last_stop_timestamp="2026-04-06T08:58:00+00:00",
        ),
        SimpleNamespace(
            name="unity-pool-ubuntu-17-staging",
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
    monkeypatch.setattr(
        vm_helpers_module,
        "_delete_pool_vm_instance",
        lambda _client, vm_name, vm_type=None: deleted_vm_names.append(
            (vm_name, vm_type),
        ),
    )

    result = vm_helpers_module.trim_stopped_pool_reserve("ubuntu")

    assert result["kept"] == [
        "unity-pool-ubuntu-14-staging",
        "unity-pool-ubuntu-15-staging",
    ]
    assert result["deleted"] == [
        "unity-pool-ubuntu-16-staging",
        "unity-pool-ubuntu-17-staging",
    ]
    assert result["actions"] == [
        "Deleted excess stopped reserve VM unity-pool-ubuntu-16-staging",
        "Deleted excess stopped reserve VM unity-pool-ubuntu-17-staging",
    ]
    assert result["errors"] == []
    assert deleted_vm_names == [
        ("unity-pool-ubuntu-16-staging", "ubuntu"),
        ("unity-pool-ubuntu-17-staging", "ubuntu"),
    ]


def test_purge_quarantined_vms_releases_network_resources(monkeypatch):
    quarantined_vm = SimpleNamespace(
        name=vm_helpers_module._pool_vm_name("windows", 12),
        status="TERMINATED",
    )
    client = MagicMock()
    client.list.return_value = [quarantined_vm]
    deleted = []

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: client,
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_delete_pool_vm_instance",
        lambda _client, vm_name, vm_type=None: deleted.append((vm_name, vm_type)),
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_log_vm_pool_event",
        lambda *_args, **_kwargs: None,
    )

    result = vm_helpers_module.purge_quarantined_vms("windows")

    assert deleted == [(quarantined_vm.name, "windows")]
    assert result == {"found": 1, "deleted": [quarantined_vm.name], "errors": []}


def test_cleanup_orphaned_pool_network_resources_deletes_old_current_env_leaks(
    monkeypatch,
):
    old_timestamp = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    recent_timestamp = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    current_ip_name = vm_helpers_module._pool_ip_name("windows", 11)
    current_vm_name = vm_helpers_module._pool_vm_name("windows", 11)
    attached_ip_name = vm_helpers_module._pool_ip_name("windows", 12)
    attached_vm_name = vm_helpers_module._pool_vm_name("windows", 12)
    foreign_suffix = (
        "-staging" if vm_helpers_module.SETTINGS.env_suffix != "-staging" else ""
    )
    foreign_ip_name = (
        f"{vm_helpers_module.pool_vm_name_prefix('windows')}"
        f"-windows-ip-77{foreign_suffix}"
    )
    legacy_ip_name = "droid-pool-ubuntu-ip-9"
    if vm_helpers_module.SETTINGS.env_suffix:
        legacy_ip_name = (
            f"droid-pool-ubuntu-ip-9{vm_helpers_module.SETTINGS.env_suffix}"
        )
    preview_ip_name = "unity-pool-ubuntu-ip-4-preview"

    instance_client = MagicMock()
    instance_client.list.return_value = [SimpleNamespace(name=attached_vm_name)]
    address_client = MagicMock()
    address_client.list.return_value = [
        SimpleNamespace(
            name=current_ip_name,
            status="RESERVED",
            users=[],
            creation_timestamp=old_timestamp,
        ),
        SimpleNamespace(
            name=attached_ip_name,
            status="RESERVED",
            users=[],
            creation_timestamp=old_timestamp,
        ),
        SimpleNamespace(
            name=vm_helpers_module._pool_ip_name("windows", 13),
            status="RESERVED",
            users=[],
            creation_timestamp=recent_timestamp,
        ),
        SimpleNamespace(
            name=foreign_ip_name,
            status="RESERVED",
            users=[],
            creation_timestamp=old_timestamp,
        ),
        SimpleNamespace(
            name=legacy_ip_name,
            status="RESERVED",
            users=[],
            creation_timestamp=old_timestamp,
        ),
        SimpleNamespace(
            name=preview_ip_name,
            status="RESERVED",
            users=[],
            creation_timestamp=old_timestamp,
        ),
    ]
    deleted_dns = []
    deleted_ips = []

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.InstancesClient",
        lambda: instance_client,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.AddressesClient",
        lambda: address_client,
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_delete_dns_a_record",
        lambda hostname: deleted_dns.append(hostname) or True,
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_delete_pool_static_ip",
        lambda ip_name: deleted_ips.append(ip_name) or True,
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_log_vm_pool_event",
        lambda *_args, **_kwargs: None,
    )

    # Windows pass should reclaim current-env windows leak only.
    windows_result = vm_helpers_module.cleanup_orphaned_pool_network_resources(
        "windows",
    )
    assert deleted_ips == [current_ip_name]
    assert deleted_dns == [
        vm_helpers_module._pool_vm_hostname(current_vm_name, "windows"),
    ]
    assert windows_result["deleted_addresses"] == [current_ip_name]

    deleted_ips.clear()
    deleted_dns.clear()

    # Ubuntu pass should reclaim historical droid-pool + retired preview leftovers.
    ubuntu_result = vm_helpers_module.cleanup_orphaned_pool_network_resources("ubuntu")
    assert set(deleted_ips) == {legacy_ip_name, preview_ip_name}
    assert set(deleted_dns) == {
        vm_helpers_module._pool_vm_hostname(
            legacy_ip_name.replace("-ip-", "-", 1),
            "ubuntu",
        ),
        vm_helpers_module._pool_vm_hostname(
            preview_ip_name.replace("-ip-", "-", 1),
            "ubuntu",
        ),
    }
    assert set(ubuntu_result["deleted_addresses"]) == {legacy_ip_name, preview_ip_name}


class _FakeDnsChanges:
    """Records one batched Cloud DNS change instead of applying it."""

    def __init__(self, applied: list[list[str]], fail: bool):
        self._applied = applied
        self._fail = fail
        self._pending: list[str] = []

    def delete_record_set(self, record):
        self._pending.append(record.name)

    def create(self):
        if self._fail:
            raise RuntimeError("cloud dns rejected the change")
        self._applied.append(list(self._pending))


class _FakeDnsZone:
    def __init__(self, records, applied: list[list[str]], fail: bool = False):
        self._records = records
        self._applied = applied
        self._fail = fail

    def list_resource_record_sets(self):
        return list(self._records)

    def changes(self):
        return _FakeDnsChanges(self._applied, self._fail)


def _dns_record(fqdn: str, record_type: str = "A"):
    return SimpleNamespace(name=fqdn, record_type=record_type)


def _assistant_address(assistant_id: str, *, operation_id: str | None = None):
    labels = (
        vm_helpers_module._assistant_rotation_labels(assistant_id, operation_id)
        if operation_id
        else vm_helpers_module.assistant_static_ip_labels(assistant_id)
    )
    name = (
        vm_helpers_module.assistant_static_ip_rotation_name(assistant_id, operation_id)
        if operation_id
        else vm_helpers_module.assistant_static_ip_name(assistant_id)
    )
    return SimpleNamespace(name=name, labels=labels)


def _install_assistant_dns_fixture(
    monkeypatch,
    *,
    addresses_by_region: dict[str, list],
    records,
    fail: bool = False,
) -> list[list[str]]:
    """Wire a fake multi-region address estate and DNS zone; return applied batches."""

    applied: list[list[str]] = []
    address_client = MagicMock()
    address_client.aggregated_list.return_value = [
        (f"regions/{region}", SimpleNamespace(addresses=addresses))
        for region, addresses in addresses_by_region.items()
    ]
    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.AddressesClient",
        lambda: address_client,
    )
    dns_client = MagicMock()
    dns_client.zone.return_value = _FakeDnsZone(records, applied, fail)
    monkeypatch.setattr(
        "communication.infra.vm_helpers.dns.Client",
        lambda project: dns_client,
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_log_vm_pool_event",
        lambda *_args, **_kwargs: None,
    )
    return applied


def test_cleanup_orphaned_assistant_dns_keeps_owned_rotating_and_foreign_env(
    monkeypatch,
):
    suffix = vm_helpers_module.SETTINGS.env_suffix
    foreign_suffix = "-staging" if suffix != "-staging" else ""
    orphan = f"unity-assistant-4001{suffix}.{vm_helpers_module.DOMAIN_SUFFIX}."
    owned = f"unity-assistant-4002{suffix}.{vm_helpers_module.DOMAIN_SUFFIX}."
    rotating = f"unity-assistant-4003{suffix}.{vm_helpers_module.DOMAIN_SUFFIX}."
    foreign = f"unity-assistant-4004{foreign_suffix}.{vm_helpers_module.DOMAIN_SUFFIX}."
    pool = f"{vm_helpers_module._pool_vm_name('ubuntu', 1)}.{vm_helpers_module.DOMAIN_SUFFIX}."

    applied = _install_assistant_dns_fixture(
        monkeypatch,
        addresses_by_region={
            "us-central1": [_assistant_address("4002")],
            # A rotating assistant owns only an operation-scoped candidate, in
            # whichever region its desktop currently lives.
            "europe-west2": [_assistant_address("4003", operation_id="op-7")],
        },
        records=[
            _dns_record(orphan),
            _dns_record(owned),
            _dns_record(rotating),
            _dns_record(foreign),
            _dns_record(pool),
            _dns_record(orphan, record_type="TXT"),
        ],
    )

    result = vm_helpers_module.cleanup_orphaned_assistant_dns_records(apply=True)

    assert applied == [[orphan]]
    assert result["found"] == 1
    assert result["deleted"] == [orphan.rstrip(".")]
    assert result["candidates"] == [orphan.rstrip(".")]
    assert result["errors"] == []


def test_cleanup_orphaned_assistant_dns_keeps_records_of_unlabeled_addresses(
    monkeypatch,
):
    """An address reserved before label repair landed still confers ownership.

    Ownership would otherwise be invisible for those addresses and their live
    records would read as orphans.
    """

    suffix = vm_helpers_module.SETTINGS.env_suffix
    unlabeled = SimpleNamespace(
        name=vm_helpers_module.assistant_static_ip_name("4006"),
        labels={},
    )
    record = f"unity-assistant-4006{suffix}.{vm_helpers_module.DOMAIN_SUFFIX}."

    applied = _install_assistant_dns_fixture(
        monkeypatch,
        addresses_by_region={"us-central1": [unlabeled]},
        records=[_dns_record(record)],
    )

    result = vm_helpers_module.cleanup_orphaned_assistant_dns_records(apply=True)

    assert applied == []
    assert result["found"] == 0


def test_cleanup_orphaned_assistant_dns_dry_run_reports_without_deleting(monkeypatch):
    suffix = vm_helpers_module.SETTINGS.env_suffix
    orphan = f"unity-assistant-4005{suffix}.{vm_helpers_module.DOMAIN_SUFFIX}."

    applied = _install_assistant_dns_fixture(
        monkeypatch,
        addresses_by_region={"us-central1": []},
        records=[_dns_record(orphan)],
    )

    result = vm_helpers_module.cleanup_orphaned_assistant_dns_records()

    assert applied == []
    assert result["applied"] is False
    assert result["candidates"] == [orphan.rstrip(".")]
    assert result["deleted"] == []


def test_cleanup_orphaned_assistant_dns_caps_and_reports_batch_failure(monkeypatch):
    suffix = vm_helpers_module.SETTINGS.env_suffix
    records = [
        _dns_record(
            f"unity-assistant-41{index:02d}{suffix}.{vm_helpers_module.DOMAIN_SUFFIX}.",
        )
        for index in range(4)
    ]
    applied = _install_assistant_dns_fixture(
        monkeypatch,
        addresses_by_region={"us-central1": []},
        records=records,
        fail=True,
    )

    result = vm_helpers_module.cleanup_orphaned_assistant_dns_records(
        apply=True,
        max_deletions=2,
    )

    assert applied == []
    assert result["found"] == 4
    assert result["truncated"] == 2
    assert result["deleted"] == []
    assert [error["resource"] for error in result["errors"]] == [
        record.name.rstrip(".") for record in records[:2]
    ]


def test_pool_identity_parsers_accept_historical_prefixes_and_retired_suffixes(
    monkeypatch,
):
    monkeypatch.setenv("DEPLOY_ENV", "production")
    assert vm_helpers_module._parse_pool_vm_identity(
        "droid-pool-ubuntu-3",
        "ubuntu",
    ) == ("droid-pool", "ubuntu", 3, "")
    assert (
        vm_helpers_module._pool_ip_name_for_vm("droid-pool-ubuntu-3", "ubuntu")
        == "droid-pool-ubuntu-ip-3"
    )
    assert (
        vm_helpers_module._pool_vm_name_from_ip_name(
            "unity-pool-windows-ip-2-preview",
            "windows",
        )
        == "unity-pool-windows-2-preview"
    )
    assert (
        vm_helpers_module._pool_vm_hostname(
            "unity-pool-windows-2-preview",
            "windows",
        )
        == "unity-pool-windows-2-preview.vm.unify.ai"
    )
    # Active foreign env must stay untouched by the production controller.
    assert (
        vm_helpers_module._pool_vm_name_from_ip_name(
            "unity-pool-ubuntu-ip-1-staging",
            "ubuntu",
        )
        is None
    )


def test_cleanup_deleted_pool_vm_network_resources_uses_historical_ip_name(
    monkeypatch,
):
    deleted_ips = []
    deleted_dns = []
    monkeypatch.setattr(
        vm_helpers_module,
        "_delete_dns_a_record",
        lambda hostname: deleted_dns.append(hostname) or True,
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_delete_pool_static_ip",
        lambda ip_name: deleted_ips.append(ip_name) or True,
    )
    monkeypatch.setattr(
        vm_helpers_module,
        "_log_vm_pool_event",
        lambda *_args, **_kwargs: None,
    )

    result = vm_helpers_module._cleanup_deleted_pool_vm_network_resources(
        "droid-pool-ubuntu-7",
        vm_type="ubuntu",
    )

    assert deleted_ips == ["droid-pool-ubuntu-ip-7"]
    assert deleted_dns == ["droid-pool-ubuntu-7.vm.unify.ai"]
    assert result["ip_deleted"] is True
    assert result["dns_deleted"] is True


def test_rebalance_pool_includes_stopped_reserve_prune_actions(monkeypatch):
    monkeypatch.setattr(
        vm_helpers_module,
        "cleanup_orphaned_pool_network_resources",
        lambda *_args, **_kwargs: {
            "actions": ["deleted orphaned IP"],
            "deleted_addresses": ["unity-pool-ubuntu-ip-18-staging"],
            "deleted_dns": ["unity-pool-ubuntu-18-staging.vm.unify.ai"],
            "errors": [],
        },
    )
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
            "deleted": ["unity-pool-ubuntu-17-staging"],
            "kept": ["unity-pool-ubuntu-14-staging"],
        },
    )

    result = vm_helpers_module.rebalance_pool("ubuntu")

    assert result == {
        "vm_type": "ubuntu",
        "actions": [
            "deleted orphaned IP",
            "scrubbed ghost",
            "replenished idle",
            "trimmed idle",
            "deleted old reserve",
        ],
        "orphaned_static_ips_deleted": ["unity-pool-ubuntu-ip-18-staging"],
        "orphaned_dns_deleted": ["unity-pool-ubuntu-18-staging.vm.unify.ai"],
        "orphaned_network_errors": [],
        "stopped_reserve_deleted": ["unity-pool-ubuntu-17-staging"],
        "stopped_reserve_kept": ["unity-pool-ubuntu-14-staging"],
    }


# ---------------------------------------------------------------------------
# reconcile_orphaned_disks — Branch A (orphan) / B (idle) / C (hard cap)
# ---------------------------------------------------------------------------


def _fake_disk(
    *,
    name: str,
    last_detach_seconds_ago: int | None,
    type_url: str = (
        "projects/gcp-project-vms/zones/us-central1-f/diskTypes/pd-standard"
    ),
    users=(),
    creation_seconds_ago: int | None = None,
):
    detach = (
        (datetime.now(UTC) - timedelta(seconds=last_detach_seconds_ago)).isoformat()
        if last_detach_seconds_ago is not None
        else ""
    )
    created = (
        datetime.now(UTC)
        - timedelta(
            seconds=(
                creation_seconds_ago
                if creation_seconds_ago is not None
                else (last_detach_seconds_ago or 0)
            ),
        )
    ).isoformat()
    return SimpleNamespace(
        name=name,
        type_=type_url,
        users=list(users),
        last_detach_timestamp=detach,
        creation_timestamp=created,
    )


def _install_disk_reconcile_env(
    monkeypatch,
    *,
    disks,
    assistant_exists_map,
    archive_info_map,
    env_suffix: str = "-staging",
):
    """Install mocks for the GCE DisksClient, Orchestra lookup, and GCS."""
    delete_calls: list[str] = []

    class _FakeClient:
        def list(self, request=None):
            return list(disks)

        def delete(self, *, project, zone, disk):
            delete_calls.append(disk)
            return SimpleNamespace(result=lambda: None)

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.DisksClient",
        lambda: _FakeClient(),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._assistant_exists",
        lambda assistant_id: assistant_exists_map.get(assistant_id, True),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._assistant_archive_info",
        lambda assistant_id: archive_info_map.get(assistant_id, (False, None)),
    )
    monkeypatch.setenv("DEPLOY_ENV", "staging" if env_suffix else "production")
    return delete_calls


def test_reconcile_deletes_orphan_after_grace(monkeypatch):
    disks = [
        _fake_disk(
            name="unity-disk-gone-assistant-staging",
            last_detach_seconds_ago=int(timedelta(hours=73).total_seconds()),
        ),
    ]
    delete_calls = _install_disk_reconcile_env(
        monkeypatch,
        disks=disks,
        assistant_exists_map={"gone-assistant": False},
        archive_info_map={},
    )

    result = vm_helpers_module.reconcile_orphaned_disks(
        max_age_hours=72,
        idle_hours=24 * 30,
        hard_cap_hours=0,
    )

    assert delete_calls == ["unity-disk-gone-assistant-staging"]
    assert result["deleted"] == 1
    assert result["deleted_orphan"] == 1
    assert result["deleted_idle"] == 0
    assert result["deleted_hard_cap"] == 0


def test_reconcile_skips_orphan_inside_grace_window(monkeypatch):
    disks = [
        _fake_disk(
            name="unity-disk-gone-assistant-staging",
            last_detach_seconds_ago=int(timedelta(hours=1).total_seconds()),
        ),
    ]
    delete_calls = _install_disk_reconcile_env(
        monkeypatch,
        disks=disks,
        assistant_exists_map={"gone-assistant": False},
        archive_info_map={},
    )

    result = vm_helpers_module.reconcile_orphaned_disks(
        max_age_hours=72,
        idle_hours=24 * 30,
        hard_cap_hours=0,
    )

    assert delete_calls == []
    assert result["deleted"] == 0
    assert result["skipped_fresh"] == 1


def test_reconcile_skips_active_assistant_under_idle_threshold(monkeypatch):
    disks = [
        _fake_disk(
            name="unity-disk-hot-assistant-staging",
            last_detach_seconds_ago=int(timedelta(hours=2).total_seconds()),
        ),
    ]
    fresh_archive = datetime.now(UTC)
    delete_calls = _install_disk_reconcile_env(
        monkeypatch,
        disks=disks,
        assistant_exists_map={"hot-assistant": True},
        archive_info_map={"hot-assistant": (True, fresh_archive)},
    )

    result = vm_helpers_module.reconcile_orphaned_disks(
        max_age_hours=72,
        idle_hours=24 * 30,
        hard_cap_hours=0,
    )

    assert delete_calls == []
    assert result["skipped_active_assistant"] == 1
    assert result["deleted_idle"] == 0


def test_reconcile_deletes_idle_assistant_with_fresh_archive(monkeypatch):
    detach_seconds = int(timedelta(days=31).total_seconds())
    disks = [
        _fake_disk(
            name="unity-disk-cold-assistant-staging",
            last_detach_seconds_ago=detach_seconds,
        ),
    ]
    detach_ts = datetime.now(UTC) - timedelta(seconds=detach_seconds)
    fresh_archive = detach_ts + timedelta(minutes=1)
    delete_calls = _install_disk_reconcile_env(
        monkeypatch,
        disks=disks,
        assistant_exists_map={"cold-assistant": True},
        archive_info_map={"cold-assistant": (True, fresh_archive)},
    )

    result = vm_helpers_module.reconcile_orphaned_disks(
        max_age_hours=72,
        idle_hours=24 * 30,
        hard_cap_hours=0,
    )

    assert delete_calls == ["unity-disk-cold-assistant-staging"]
    assert result["deleted_idle"] == 1
    assert result["deleted_orphan"] == 0


def test_reconcile_keeps_idle_disk_when_archive_missing(monkeypatch):
    disks = [
        _fake_disk(
            name="unity-disk-cold-assistant-staging",
            last_detach_seconds_ago=int(timedelta(days=45).total_seconds()),
        ),
    ]
    delete_calls = _install_disk_reconcile_env(
        monkeypatch,
        disks=disks,
        assistant_exists_map={"cold-assistant": True},
        archive_info_map={"cold-assistant": (False, None)},
    )

    result = vm_helpers_module.reconcile_orphaned_disks(
        max_age_hours=72,
        idle_hours=24 * 30,
        hard_cap_hours=0,
    )

    assert delete_calls == []
    assert result["skipped_no_archive"] == 1
    assert result["deleted_idle"] == 0


def test_reconcile_keeps_idle_disk_when_archive_older_than_detach(monkeypatch):
    detach_seconds = int(timedelta(days=45).total_seconds())
    disks = [
        _fake_disk(
            name="unity-disk-cold-assistant-staging",
            last_detach_seconds_ago=detach_seconds,
        ),
    ]
    detach_ts = datetime.now(UTC) - timedelta(seconds=detach_seconds)
    stale_archive = detach_ts - timedelta(hours=12)
    delete_calls = _install_disk_reconcile_env(
        monkeypatch,
        disks=disks,
        assistant_exists_map={"cold-assistant": True},
        archive_info_map={"cold-assistant": (True, stale_archive)},
    )

    result = vm_helpers_module.reconcile_orphaned_disks(
        max_age_hours=72,
        idle_hours=24 * 30,
        hard_cap_hours=0,
    )

    assert delete_calls == []
    assert result["skipped_stale_archive"] == 1


def test_reconcile_hard_cap_deletes_when_enabled(monkeypatch):
    detach_seconds = int(timedelta(days=200).total_seconds())
    disks = [
        _fake_disk(
            name="unity-disk-very-cold-staging",
            last_detach_seconds_ago=detach_seconds,
        ),
    ]
    delete_calls = _install_disk_reconcile_env(
        monkeypatch,
        disks=disks,
        assistant_exists_map={"very-cold": True},
        archive_info_map={"very-cold": (False, None)},
    )

    result = vm_helpers_module.reconcile_orphaned_disks(
        max_age_hours=72,
        idle_hours=24 * 30,
        hard_cap_hours=24 * 180,
    )

    assert delete_calls == ["unity-disk-very-cold-staging"]
    assert result["deleted_hard_cap"] == 1
    assert result["skipped_no_archive"] == 0


def test_reconcile_ignores_non_assistant_and_attached_disks(monkeypatch):
    old_detach = int(timedelta(days=60).total_seconds())
    disks = [
        _fake_disk(
            name="some-other-disk",
            last_detach_seconds_ago=old_detach,
        ),
        _fake_disk(
            name="unity-disk-attached-staging",
            last_detach_seconds_ago=old_detach,
            users=[
                "projects/gcp-project-vms/zones/us-central1-f/instances/unity-pool-ubuntu-3",
            ],
        ),
        _fake_disk(
            name="unity-disk-ssd-pool-staging",
            last_detach_seconds_ago=old_detach,
            type_url=(
                "projects/gcp-project-vms/zones/us-central1-f/" "diskTypes/pd-ssd"
            ),
        ),
    ]
    delete_calls = _install_disk_reconcile_env(
        monkeypatch,
        disks=disks,
        assistant_exists_map={},
        archive_info_map={},
    )

    result = vm_helpers_module.reconcile_orphaned_disks(
        max_age_hours=72,
        idle_hours=24 * 30,
        hard_cap_hours=24 * 7,
    )

    assert delete_calls == []
    assert result["deleted"] == 0


def test_reconcile_records_delete_race_as_error(monkeypatch):
    detach_seconds = int(timedelta(days=45).total_seconds())
    disks = [
        _fake_disk(
            name="unity-disk-racing-staging",
            last_detach_seconds_ago=detach_seconds,
        ),
    ]
    detach_ts = datetime.now(UTC) - timedelta(seconds=detach_seconds)
    fresh_archive = detach_ts + timedelta(minutes=5)

    class _RacingClient:
        def list(self, request=None):
            return list(disks)

        def delete(self, *, project, zone, disk):
            raise RuntimeError("disk is attached")

    monkeypatch.setattr(
        "communication.infra.vm_helpers.compute_v1.DisksClient",
        lambda: _RacingClient(),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._assistant_exists",
        lambda assistant_id: True,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._assistant_archive_info",
        lambda assistant_id: (True, fresh_archive),
    )
    monkeypatch.setenv("DEPLOY_ENV", "staging")

    result = vm_helpers_module.reconcile_orphaned_disks(
        max_age_hours=72,
        idle_hours=24 * 30,
        hard_cap_hours=0,
    )

    assert result["deleted"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["disk"] == "unity-disk-racing-staging"


def test_authenticated_probe_verifies_tls(monkeypatch):
    """The bearer token must only go out over a verified TLS connection.

    ``/api/exec`` runs commands on the VM, so a probe that skipped
    verification would hand the session key to whatever answers the DNS name.
    """
    captured = {}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(vm_helpers_module.requests, "post", _fake_post)

    assert vm_helpers_module.probe_vm_agent_service_authenticated(
        "unity-pool-ubuntu-2.vm.unify.ai",
        "session-key-123",
    )

    assert captured["url"].startswith("https://")
    assert captured["kwargs"].get("verify", True) is True
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer session-key-123"


def test_authenticated_probe_not_ready_when_tls_untrusted(monkeypatch):
    """A VM missing the wildcard cert reads as not-ready, not as an error."""

    def _fake_post(*_args, **_kwargs):
        raise vm_helpers_module.requests.exceptions.SSLError("self-signed certificate")

    monkeypatch.setattr(vm_helpers_module.requests, "post", _fake_post)

    assert not vm_helpers_module.probe_vm_agent_service_authenticated(
        "unity-pool-ubuntu-2.vm.unify.ai",
        "session-key-123",
    )


def test_authenticated_probe_skips_request_without_key(monkeypatch):
    """No key means nothing to verify — do not touch the VM at all."""

    def _fake_post(*_args, **_kwargs):
        raise AssertionError("probe must not call the VM without an api_key")

    monkeypatch.setattr(vm_helpers_module.requests, "post", _fake_post)

    assert not vm_helpers_module.probe_vm_agent_service_authenticated(
        "unity-pool-ubuntu-2.vm.unify.ai",
        "",
    )
