from communication.assistant_session_controller import pool_controller
from communication.infra import vm_helpers
from common.settings import SETTINGS


def _session(
    assistant_id: str,
    *,
    phase: str,
    desktop_required: bool = True,
    desktop_mode: str = "ubuntu",
    desired_state: str = "Running",
    has_vm: bool = False,
    has_job: bool = False,
    placement: dict | None = None,
) -> dict:
    binding = {"id": f"binding-{assistant_id}"}
    if has_vm:
        binding["vmRef"] = {"name": f"vm-{assistant_id}"}
    if has_job:
        binding["jobRef"] = {"name": f"job-{assistant_id}"}
    desktop = {"required": desktop_required, "mode": desktop_mode}
    if placement is not None:
        desktop["placement"] = placement
    return {
        "spec": {
            "assistantId": assistant_id,
            "desiredState": desired_state,
            "desktop": desktop,
        },
        "status": {
            "phase": phase,
            "binding": binding,
        },
    }


def test_pending_vm_demand_counts_only_waiting_sessions():
    sessions = [
        _session("1", phase="PendingVM", desktop_mode="ubuntu"),
        _session("2", phase="PendingVM", desktop_mode="windows"),
        _session("3", phase="Active", desktop_mode="ubuntu"),
        _session("4", phase="PendingVM", desktop_required=False),
        _session("5", phase="PendingVM", desired_state="Stopped"),
        _session("6", phase="PendingVM", has_vm=True),
    ]

    demand = pool_controller.pending_vm_demand(sessions)

    assert demand == {"ubuntu": 1, "windows": 1}


def test_pending_job_demand_counts_only_waiting_sessions():
    sessions = [
        _session("1", phase="PendingJob"),
        _session("2", phase="PendingContainer"),
        _session("3", phase="PendingJob", desired_state="Stopped"),
        _session("4", phase="PendingJob", has_job=True),
    ]

    demand = pool_controller.pending_job_demand(sessions)

    assert demand == 1


def test_reconcile_pool_once_uses_pending_demand(monkeypatch):
    replenish_calls = []
    trim_calls = []
    job_replenish_calls = []

    class FakeCustomApi:
        def list_namespaced_custom_object(self, **_kwargs):
            return {
                "items": [
                    _session("job-1", phase="PendingJob"),
                    _session("1", phase="PendingVM", desktop_mode="ubuntu"),
                    _session("2", phase="PendingVM", desktop_mode="ubuntu"),
                ],
            }

    monkeypatch.setattr(
        pool_controller,
        "replenish_pool",
        lambda vm_type, extra_demand=0: replenish_calls.append(
            (vm_type, extra_demand),
        )
        or {"vm_type": vm_type, "extra_demand": extra_demand},
    )
    monkeypatch.setattr(
        pool_controller,
        "trim_pool",
        lambda vm_type: trim_calls.append(vm_type) or {"vm_type": vm_type},
    )
    monkeypatch.setattr(
        pool_controller,
        "emit_observability_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        pool_controller,
        "_get_current_image_hash",
        lambda: None,
    )
    monkeypatch.setattr(
        pool_controller,
        "schedule_idle_job_pool_replenishment",
        lambda extra_demand, source: job_replenish_calls.append(
            (extra_demand, source),
        )
        or True,
    )

    result = pool_controller.reconcile_pool_once(FakeCustomApi(), "staging")

    assert job_replenish_calls == [(1, "controller.pool_reconcile")]
    assert replenish_calls == [("ubuntu", 2), ("windows", 0)]
    assert trim_calls == ["windows"]
    assert result["unity_jobs"] == {
        "pending_sessions": 1,
        "replenish_extra_demand": 1,
        "replenish_scheduled": True,
    }
    assert result["ubuntu"]["pending_sessions"] == 2
    assert result["windows"]["pending_sessions"] == 0
    assert result["vm_pools"]["ubuntu:legacy"]["pending_sessions"] == 2


def test_reconcile_pool_once_schedules_replenish_for_hash_deficit(monkeypatch):
    job_replenish_calls = []

    class FakeCustomApi:
        def list_namespaced_custom_object(self, **_kwargs):
            return {"items": []}

    monkeypatch.setattr(
        pool_controller,
        "_get_current_image_hash",
        lambda: "newhash",
    )
    monkeypatch.setattr(
        pool_controller,
        "_count_idle_jobs_by_hash",
        lambda _current_hash: (0, 6),
    )
    monkeypatch.setattr(
        pool_controller,
        "replenish_pool",
        lambda vm_type, extra_demand=0: {"vm_type": vm_type},
    )
    monkeypatch.setattr(pool_controller, "trim_pool", lambda vm_type: None)
    monkeypatch.setattr(
        pool_controller,
        "emit_observability_event",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        pool_controller,
        "schedule_idle_job_pool_replenishment",
        lambda extra_demand, source, refresh=False: job_replenish_calls.append(
            (extra_demand, source, refresh),
        )
        or True,
    )

    result = pool_controller.reconcile_pool_once(FakeCustomApi(), "staging")

    assert job_replenish_calls == [(3, "controller.pool_reconcile", False)]
    assert result["unity_jobs"]["replenish_extra_demand"] == 3


def test_reconcile_pool_once_replenishes_each_regional_pool(monkeypatch):
    placement = {
        "poolLocation": "europe-west2",
        "region": "europe-west2",
        "zone": "europe-west2-a",
    }
    replenished = []

    class FakeCustomApi:
        def list_namespaced_custom_object(self, **_kwargs):
            return {
                "items": [
                    _session("legacy", phase="PendingVM"),
                    _session("europe", phase="PendingVM", placement=placement),
                ],
            }

    monkeypatch.setattr(
        pool_controller,
        "replenish_pool",
        lambda vm_type, extra_demand=0: replenished.append(
            (
                vm_type,
                extra_demand,
                vm_helpers._current_vm_placement().region,
                vm_helpers._current_vm_placement().zone,
            ),
        )
        or {"vm_type": vm_type},
    )
    monkeypatch.setattr(pool_controller, "trim_pool", lambda _vm_type: {})
    monkeypatch.setattr(
        pool_controller,
        "emit_observability_event",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(pool_controller, "_get_current_image_hash", lambda: None)

    result = pool_controller.reconcile_pool_once(FakeCustomApi(), "staging")

    assert ("ubuntu", 1, SETTINGS.vm_region, SETTINGS.vm_zone) in replenished
    assert ("ubuntu", 1, "europe-west2", "europe-west2-a") in replenished
    assert (
        result["vm_pools"]["ubuntu:europe-west2:europe-west2-a"]["pending_sessions"]
        == 1
    )
