from communication.assistant_session_controller import pool_controller


def _session(
    assistant_id: str,
    *,
    phase: str,
    desktop_required: bool = True,
    desktop_mode: str = "ubuntu",
    desired_state: str = "Running",
    has_vm: bool = False,
    has_job: bool = False,
) -> dict:
    binding = {"id": f"binding-{assistant_id}"}
    if has_vm:
        binding["vmRef"] = {"name": f"vm-{assistant_id}"}
    if has_job:
        binding["jobRef"] = {"name": f"job-{assistant_id}"}
    return {
        "spec": {
            "assistantId": assistant_id,
            "desiredState": desired_state,
            "desktop": {"required": desktop_required, "mode": desktop_mode},
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
    assert result["droid_jobs"] == {
        "pending_sessions": 1,
        "replenish_scheduled": True,
    }
    assert result["ubuntu"]["pending_sessions"] == 2
    assert result["windows"]["pending_sessions"] == 0
