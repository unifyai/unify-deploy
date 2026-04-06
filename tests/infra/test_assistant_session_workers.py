from threading import Event
from unittest.mock import MagicMock

from communication.assistant_session_controller import workers


def _session(*, binding_id: str = "binding-1", desired_state: str = "Running") -> dict:
    return {
        "spec": {"desiredState": desired_state},
        "status": {"binding": {"id": binding_id}},
    }


def test_task_runtime_deduplicates_inflight_work(monkeypatch):
    runtime = workers._TaskRuntime(max_workers=1)
    started = Event()
    release = Event()

    monkeypatch.setattr(
        workers,
        "emit_observability_event",
        lambda *_args, **_kwargs: None,
    )

    def block():
        started.set()
        release.wait(timeout=5)

    try:
        assert runtime.submit(
            task_type="vm_assignment",
            task_assistant_id="1207",
            task_binding_id="binding-1",
            fn=block,
        )
        assert started.wait(timeout=1)
        assert not runtime.submit(
            task_type="vm_assignment",
            task_assistant_id="1207",
            task_binding_id="binding-1",
            fn=block,
        )
    finally:
        release.set()
        runtime._executor.shutdown(wait=True)

    assert runtime.stats() == {"inflight": 0}


def test_run_vm_assignment_records_assigned_signal(monkeypatch):
    custom_api = object()
    record_signal = MagicMock()

    monkeypatch.setattr(
        workers,
        "get_assistant_session",
        lambda *_args, **_kwargs: _session(),
    )
    monkeypatch.setattr(
        workers,
        "read_bootstrap_secret",
        lambda *_args, **_kwargs: {"api_key": "user-key"},
    )
    monkeypatch.setattr(
        workers,
        "assign_pool_vm",
        lambda **_kwargs: {
            "vm_name": "unity-pool-ubuntu-1-preview",
            "hostname": "vm-1.vm.unify.ai",
            "desktop_url": "https://vm-1.vm.unify.ai",
        },
    )
    monkeypatch.setattr(workers, "record_assistant_session_signal", record_signal)

    workers._run_vm_assignment(
        custom_api=custom_api,
        core_api=object(),
        namespace="preview",
        assistant_id="1207",
        binding_id="binding-1",
        secret_name="assistant-session-bootstrap-1207",
        vm_type="ubuntu",
    )

    assert record_signal.call_args.args[:3] == (custom_api, "preview", "1207")
    assert record_signal.call_args.kwargs["signal_name"] == workers.SIGNAL_VM_ASSIGNMENT
    payload = record_signal.call_args.kwargs["payload"]
    assert payload["bindingId"] == "binding-1"
    assert payload["state"] == "assigned"
    assert payload["vmRef"] == {
        "name": "unity-pool-ubuntu-1-preview",
        "hostname": "vm-1.vm.unify.ai",
        "vmType": "ubuntu",
    }
    assert payload["desktopUrl"] == "https://vm-1.vm.unify.ai"
    assert payload["observedAt"]
    assert record_signal.call_args.kwargs["source"] == "worker.vm_assignment"


def test_run_vm_assignment_records_capacity_signal(monkeypatch):
    record_signal = MagicMock()
    replenish_pool = MagicMock()

    monkeypatch.setattr(
        workers,
        "get_assistant_session",
        lambda *_args, **_kwargs: _session(),
    )
    monkeypatch.setattr(
        workers,
        "read_bootstrap_secret",
        lambda *_args, **_kwargs: {"api_key": "user-key"},
    )
    monkeypatch.setattr(
        workers,
        "assign_pool_vm",
        MagicMock(side_effect=ValueError("Waiting for VM capacity")),
    )
    monkeypatch.setattr(workers, "replenish_pool", replenish_pool)
    monkeypatch.setattr(workers, "record_assistant_session_signal", record_signal)

    workers._run_vm_assignment(
        custom_api=object(),
        core_api=object(),
        namespace="preview",
        assistant_id="1207",
        binding_id="binding-1",
        secret_name="assistant-session-bootstrap-1207",
        vm_type="ubuntu",
    )

    replenish_pool.assert_called_once_with("ubuntu")
    assert record_signal.call_args.kwargs["signal_name"] == workers.SIGNAL_VM_ASSIGNMENT
    assert record_signal.call_args.kwargs["payload"]["state"] == "capacity"
    assert (
        record_signal.call_args.kwargs["payload"]["message"]
        == "Waiting for VM capacity"
    )


def test_run_guest_health_probe_records_ready_signal(monkeypatch):
    record_signal = MagicMock()
    vm_ref = {"name": "unity-pool-ubuntu-1-preview", "hostname": "vm-1.vm.unify.ai"}

    monkeypatch.setattr(
        workers,
        "get_assistant_session",
        lambda *_args, **_kwargs: _session(),
    )
    monkeypatch.setattr(
        workers,
        "probe_vm_agent_service",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(workers, "record_assistant_session_signal", record_signal)

    workers._run_guest_health_probe(
        custom_api=object(),
        namespace="preview",
        assistant_id="1207",
        binding_id="binding-1",
        vm_ref=vm_ref,
    )

    assert (
        record_signal.call_args.kwargs["signal_name"] == workers.SIGNAL_VM_GUEST_HEALTH
    )
    assert record_signal.call_args.kwargs["payload"]["state"] == "ready"
    assert record_signal.call_args.kwargs["payload"]["vmRef"] == vm_ref


def test_run_vm_release_request_records_requested_signal(monkeypatch):
    record_signal = MagicMock()

    monkeypatch.setattr(
        workers,
        "get_assistant_session",
        lambda *_args, **_kwargs: _session(),
    )
    monkeypatch.setattr(
        workers,
        "release_pool_vm",
        lambda *_args, **_kwargs: {"released": True, "pool_role": "releasing"},
    )
    monkeypatch.setattr(workers, "record_assistant_session_signal", record_signal)

    workers._run_vm_release_request(
        custom_api=object(),
        namespace="preview",
        assistant_id="1207",
        binding_id="binding-1",
        vm_name="unity-pool-ubuntu-1-preview",
    )

    assert (
        record_signal.call_args.kwargs["signal_name"]
        == workers.SIGNAL_VM_RELEASE_REQUEST
    )
    assert record_signal.call_args.kwargs["payload"]["state"] == "requested"
    assert (
        record_signal.call_args.kwargs["payload"]["vmName"]
        == "unity-pool-ubuntu-1-preview"
    )


def test_run_vm_release_request_skips_stale_binding(monkeypatch):
    record_signal = MagicMock()

    monkeypatch.setattr(
        workers,
        "get_assistant_session",
        lambda *_args, **_kwargs: _session(binding_id="binding-2"),
    )
    monkeypatch.setattr(workers, "record_assistant_session_signal", record_signal)

    workers._run_vm_release_request(
        custom_api=object(),
        namespace="preview",
        assistant_id="1207",
        binding_id="binding-1",
        vm_name="unity-pool-ubuntu-1-preview",
    )

    record_signal.assert_not_called()
