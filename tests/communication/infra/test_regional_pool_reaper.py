from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from communication.infra import vm_helpers
from communication.infra.gcp_region_catalog import PoolLocation, VmPlacement


def _location() -> PoolLocation:
    return PoolLocation(
        id="us-east1",
        region="us-east1",
        display_name="South Carolina",
        latitude=33.0,
        longitude=-80.0,
        zones=("us-east1-b",),
    )


def _vm(name: str, role: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        labels={"pool-role": role, "vm-type": "ubuntu"},
    )


def _install_reaper_state(monkeypatch, initial: dict):
    state = dict(initial)

    monkeypatch.setattr(
        vm_helpers,
        "_regional_pool_reaper_state",
        lambda _region, **_kwargs: dict(state),
    )

    def set_state(_region, value, **_kwargs):
        state.clear()
        state.update(value)

    monkeypatch.setattr(vm_helpers, "_set_regional_reaper_state", set_state)
    return state


def test_regional_reaper_starts_durable_grace_before_deleting(monkeypatch):
    client = MagicMock()
    client.list.return_value = []
    state = _install_reaper_state(monkeypatch, {})
    monkeypatch.setattr(vm_helpers, "list_pool_locations", lambda: (_location(),))
    monkeypatch.setattr(vm_helpers.compute_v1, "InstancesClient", lambda: client)

    result = vm_helpers.reap_inactive_regional_pools(
        core_api=object(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert result == {"regions": [{"region": "us-east1", "action": "grace_started"}]}
    assert state == {
        "emptySince": "2026-01-01T00:00:00+00:00",
        "fenced": False,
    }


def test_regional_reaper_fences_then_deletes_only_reapable_pool_vms(monkeypatch):
    idle = _vm("unity-pool-ubuntu-1", "idle")
    client = MagicMock()
    client.list.side_effect = [[idle], [idle]]
    state = _install_reaper_state(
        monkeypatch,
        {
            "emptySince": (
                datetime.now(UTC) - timedelta(hours=1, seconds=1)
            ).isoformat(),
            "fenced": False,
        },
    )
    deleted = []
    monkeypatch.setattr(vm_helpers, "list_pool_locations", lambda: (_location(),))
    monkeypatch.setattr(vm_helpers.compute_v1, "InstancesClient", lambda: client)
    monkeypatch.setattr(vm_helpers, "_set_pool_labels", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        vm_helpers,
        "_delete_pool_vm_instance",
        lambda _client, name, **_kwargs: deleted.append(name),
    )

    result = vm_helpers.reap_inactive_regional_pools(core_api=object())

    assert result["regions"] == [
        {"region": "us-east1", "action": "reaped", "deleted": ["unity-pool-ubuntu-1"]}
    ]
    assert state["fenced"] is True
    assert deleted == ["unity-pool-ubuntu-1"]


def test_regional_reaper_never_deletes_an_assigned_vm(monkeypatch):
    assigned = _vm("unity-pool-ubuntu-1", "assigned")
    client = MagicMock()
    client.list.return_value = [assigned]
    state = _install_reaper_state(
        monkeypatch,
        {"emptySince": "2026-01-01T00:00:00+00:00", "fenced": True},
    )
    delete = MagicMock()
    monkeypatch.setattr(vm_helpers, "list_pool_locations", lambda: (_location(),))
    monkeypatch.setattr(vm_helpers.compute_v1, "InstancesClient", lambda: client)
    monkeypatch.setattr(vm_helpers, "_delete_pool_vm_instance", delete)

    result = vm_helpers.reap_inactive_regional_pools(core_api=object())

    assert result["regions"] == [
        {"region": "us-east1", "action": "active", "vm_count": 1}
    ]
    assert state == {"fenced": False}
    delete.assert_not_called()


def test_assignment_clears_nonlegacy_reaper_fence(monkeypatch):
    placement = VmPlacement(_location(), "us-east1-b", "", "test")
    clear_fence = MagicMock()
    assigned = {"vm_name": "unity-pool-ubuntu-1"}
    monkeypatch.setattr(vm_helpers, "clear_regional_pool_reaper_fence", clear_fence)
    monkeypatch.setattr(vm_helpers, "_assign_pool_vm", lambda **_kwargs: assigned)

    result = vm_helpers.assign_pool_vm(
        "assistant-1",
        "binding-1",
        "api-key",
        placement=placement,
    )

    assert result == assigned
    clear_fence.assert_called_once_with(placement)


def test_replenish_skips_a_fenced_nonlegacy_pool(monkeypatch):
    monkeypatch.setattr(vm_helpers, "_regional_pool_reaper_fenced", lambda: True)

    assert vm_helpers.replenish_pool("ubuntu") == {
        "vm_type": "ubuntu",
        "actions": [],
        "skipped": True,
        "fenced": True,
    }
