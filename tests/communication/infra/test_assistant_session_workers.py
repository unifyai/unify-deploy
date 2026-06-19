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


def test_run_vm_assignment_persists_assigned_result(monkeypatch):
    custom_api = object()
    persist_result = MagicMock(return_value=True)

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
            "vm_name": "droid-pool-ubuntu-1-staging",
            "hostname": "vm-1.vm.unify.ai",
            "desktop_url": "https://vm-1.vm.unify.ai",
        },
    )
    monkeypatch.setattr(
        workers,
        "persist_binding_vm_assignment_result",
        persist_result,
    )

    workers._run_vm_assignment(
        custom_api=custom_api,
        core_api=object(),
        namespace="staging",
        assistant_id="1207",
        binding_id="binding-1",
        attempt_id="attempt-1",
        secret_name="assistant-session-bootstrap-1207",
        vm_type="ubuntu",
    )

    assert persist_result.call_args.args[:3] == (custom_api, "staging", "1207")
    assert persist_result.call_args.kwargs["target_binding_id"] == "binding-1"
    assert persist_result.call_args.kwargs["attempt_id"] == "attempt-1"
    assert persist_result.call_args.kwargs["state"] == "assigned"
    assert persist_result.call_args.kwargs["vm_ref"] == {
        "name": "droid-pool-ubuntu-1-staging",
        "hostname": "vm-1.vm.unify.ai",
        "vmType": "ubuntu",
    }
    assert persist_result.call_args.kwargs["source"] == "worker.vm_assignment"


def test_run_vm_assignment_releases_stale_successful_result(monkeypatch):
    persist_result = MagicMock(return_value=False)
    release_pool_vm = MagicMock(return_value={"released": True})

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
            "vm_name": "droid-pool-ubuntu-1-staging",
            "hostname": "vm-1.vm.unify.ai",
            "desktop_url": "https://vm-1.vm.unify.ai",
        },
    )
    monkeypatch.setattr(
        workers,
        "persist_binding_vm_assignment_result",
        persist_result,
    )
    monkeypatch.setattr(workers, "release_pool_vm", release_pool_vm)

    workers._run_vm_assignment(
        custom_api=object(),
        core_api=object(),
        namespace="staging",
        assistant_id="1207",
        binding_id="binding-1",
        attempt_id="attempt-1",
        secret_name="assistant-session-bootstrap-1207",
        vm_type="ubuntu",
    )

    release_pool_vm.assert_called_once_with(
        "1207",
        "binding-1",
        vm_name="droid-pool-ubuntu-1-staging",
    )


def test_run_vm_assignment_persists_capacity_result(monkeypatch):
    persist_result = MagicMock()
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
    monkeypatch.setattr(
        workers,
        "persist_binding_vm_assignment_result",
        persist_result,
    )

    workers._run_vm_assignment(
        custom_api=object(),
        core_api=object(),
        namespace="staging",
        assistant_id="1207",
        binding_id="binding-1",
        attempt_id="attempt-1",
        secret_name="assistant-session-bootstrap-1207",
        vm_type="ubuntu",
    )

    replenish_pool.assert_called_once_with("ubuntu")
    assert persist_result.call_args.kwargs["target_binding_id"] == "binding-1"
    assert persist_result.call_args.kwargs["attempt_id"] == "attempt-1"
    assert persist_result.call_args.kwargs["state"] == "capacity"
    assert persist_result.call_args.kwargs["message"] == "Waiting for VM capacity"


def test_run_guest_health_probe_records_ready_signal(monkeypatch):
    record_signal = MagicMock()
    vm_ref = {"name": "droid-pool-ubuntu-1-staging", "hostname": "vm-1.vm.unify.ai"}

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
        namespace="staging",
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
        namespace="staging",
        assistant_id="1207",
        binding_id="binding-1",
        vm_name="droid-pool-ubuntu-1-staging",
        release_generation=1,
    )

    assert (
        record_signal.call_args.kwargs["signal_name"]
        == workers.SIGNAL_VM_RELEASE_REQUEST
    )
    assert record_signal.call_args.kwargs["payload"]["state"] == "requested"
    assert (
        record_signal.call_args.kwargs["payload"]["vmName"]
        == "droid-pool-ubuntu-1-staging"
    )
    assert record_signal.call_args.kwargs["payload"]["releaseGeneration"] == 1


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
        namespace="staging",
        assistant_id="1207",
        binding_id="binding-1",
        vm_name="droid-pool-ubuntu-1-staging",
        release_generation=1,
    )

    record_signal.assert_not_called()
