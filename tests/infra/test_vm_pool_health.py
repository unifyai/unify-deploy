from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from communication.infra import vm_helpers as vm_helpers_module
from communication.infra.vm_helpers import (
    _claim_idle_vm_inner,
    _is_stale_inflight_vm,
    _start_one_stopped_vm,
    replenish_pool,
)


def _fake_vm(*, role: str, age_seconds: int, status: str = "RUNNING"):
    started_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return SimpleNamespace(
        labels={"pool-role": role},
        status=status,
        last_start_timestamp=started_at.isoformat(),
        creation_timestamp=started_at.isoformat(),
    )


def test_is_stale_inflight_vm_detects_old_starting_vm():
    vm = _fake_vm(role="starting", age_seconds=1200)
    assert _is_stale_inflight_vm(vm, timeout_seconds=600)


def test_is_stale_inflight_vm_ignores_recent_starting_vm():
    vm = _fake_vm(role="starting", age_seconds=120)
    assert not _is_stale_inflight_vm(vm, timeout_seconds=600)


def test_is_stale_inflight_vm_ignores_non_inflight_roles():
    vm = _fake_vm(role="stopped", age_seconds=1200, status="TERMINATED")
    assert not _is_stale_inflight_vm(vm, timeout_seconds=600)


def test_start_one_stopped_vm_returns_after_start_request(monkeypatch):
    vm = SimpleNamespace(
        name="unity-pool-ubuntu-6-preview",
        labels={"vm-type": "ubuntu"},
    )
    client = MagicMock()
    client.start.return_value = SimpleNamespace(
        name="operation-123",
        result=lambda: (_ for _ in ()).throw(AssertionError("should not wait")),
    )

    monkeypatch.setattr(
        "communication.infra.vm_helpers._set_pool_labels",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers.get_secret",
        lambda *_args, **_kwargs: "",
    )

    assert _start_one_stopped_vm(client, vm)
    client.start.assert_called_once()


def test_claim_idle_vm_quarantines_unhealthy_candidate_before_claim(monkeypatch):
    def _pool_vm(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            name=name,
            labels={"pool-role": "idle", "vm-type": "ubuntu"},
            label_fingerprint=f"{name}-fp",
            network_interfaces=[],
            metadata=SimpleNamespace(
                items=[SimpleNamespace(key="hostname", value=f"{name}.example.com")],
            ),
        )

    bad_vm = _pool_vm("unity-pool-ubuntu-2-preview")
    good_vm = _pool_vm("unity-pool-ubuntu-4-preview")
    client = MagicMock()
    client.list.side_effect = [[bad_vm], [good_vm]]
    client.get.side_effect = [bad_vm, good_vm]
    client.set_labels.return_value = SimpleNamespace(result=lambda: None)

    quarantined = []

    monkeypatch.setattr(
        "communication.infra.vm_helpers.random.choice",
        lambda vms: vms[0],
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._probe_vm_https",
        lambda hostname, timeout=2.0: hostname.startswith(good_vm.name),
    )
    monkeypatch.setattr(
        "communication.infra.vm_helpers._quarantine_pool_vm",
        lambda _client, vm, reason: quarantined.append((vm.name, reason))
        or "quarantined",
    )

    claimed = _claim_idle_vm_inner(
        client,
        label_filter="labels.pool-role = idle",
        assistant_id="assistant-123",
        vm_type="ubuntu",
        vm_number=None,
    )

    assert quarantined == [
        ("unity-pool-ubuntu-2-preview", "failed health probe during claim"),
    ]
    assert claimed["vm_name"] == "unity-pool-ubuntu-4-preview"


def test_replenish_pool_hot_path_skips_bulk_idle_health_sweep(monkeypatch):
    stopped_vm = SimpleNamespace(
        name="unity-pool-ubuntu-6-preview",
        labels={"vm-type": "ubuntu"},
    )

    monkeypatch.setattr(vm_helpers_module, "POOL_TARGET_IDLE", 1)
    monkeypatch.setattr(vm_helpers_module, "POOL_TARGET_STOPPED", 1)
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
