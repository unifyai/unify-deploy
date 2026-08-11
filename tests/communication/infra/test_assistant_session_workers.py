from threading import Event
from unittest.mock import MagicMock

from communication.assistant_session_controller import workers

_PLACEMENT = {
    "poolLocation": "us-central1",
    "region": "us-central1",
    "zone": "us-central1-a",
}


def _session(
    *,
    binding_id: str = "binding-1",
    desired_state: str = "Running",
    placement: dict | None = None,
    binding: dict | None = None,
) -> dict:
    """A session the assignment worker will actually act on.

    ``spec.desktop.placement`` is mandatory: the worker refuses to assign
    against the default pool location, so a session without it can only ever
    produce a ``capacity`` result.
    """
    return {
        "spec": {
            "desiredState": desired_state,
            "desktop": {
                "required": True,
                "mode": "ubuntu",
                "placement": _PLACEMENT if placement is None else placement,
            },
        },
        "status": {"binding": binding or {"id": binding_id}},
    }


def _assigned_vm_result() -> dict:
    """The shape ``assign_pool_vm`` returns, including its resolved placement."""
    return {
        "vm_name": "unity-pool-ubuntu-1-staging",
        "hostname": "vm-1.vm.unify.ai",
        "desktop_url": "https://vm-1.vm.unify.ai",
        "pool_location": _PLACEMENT["poolLocation"],
        "region": _PLACEMENT["region"],
        "zone": _PLACEMENT["zone"],
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
        lambda **_kwargs: _assigned_vm_result(),
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
    # The persisted vmRef carries the routing the assignment resolved, so a
    # later release/probe can find the VM without re-deriving its placement.
    assert persist_result.call_args.kwargs["vm_ref"] == {
        "name": "unity-pool-ubuntu-1-staging",
        "hostname": "vm-1.vm.unify.ai",
        "vmType": "ubuntu",
        "poolLocation": _PLACEMENT["poolLocation"],
        "region": _PLACEMENT["region"],
        "zone": _PLACEMENT["zone"],
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
        lambda **_kwargs: _assigned_vm_result(),
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

    release_pool_vm.assert_called_once()
    assert release_pool_vm.call_args.args == ("1207", "binding-1")
    assert release_pool_vm.call_args.kwargs["vm_name"] == "unity-pool-ubuntu-1-staging"
    # The stale-result release must target the placement it assigned into.
    assert release_pool_vm.call_args.kwargs["placement"].zone == _PLACEMENT["zone"]


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
    vm_ref = {"name": "unity-pool-ubuntu-1-staging", "hostname": "vm-1.vm.unify.ai"}

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
        vm_name="unity-pool-ubuntu-1-staging",
        release_generation=1,
    )

    assert (
        record_signal.call_args.kwargs["signal_name"]
        == workers.SIGNAL_VM_RELEASE_REQUEST
    )
    assert record_signal.call_args.kwargs["payload"]["state"] == "requested"
    assert (
        record_signal.call_args.kwargs["payload"]["vmName"]
        == "unity-pool-ubuntu-1-staging"
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
        vm_name="unity-pool-ubuntu-1-staging",
        release_generation=1,
    )

    record_signal.assert_not_called()


def _desktop_ready_probe_patches(monkeypatch, *, probe_ok: bool):
    """Wire the readiness poll's collaborators; return (record, publish) mocks."""
    record_signal = MagicMock()
    publish = MagicMock(return_value="message-777")

    monkeypatch.setattr(
        workers,
        "get_assistant_session",
        lambda *_args, **_kwargs: _session(
            binding={"id": "binding-1", "desktopSecret": "vm-secret-abc"},
        ),
    )
    monkeypatch.setattr(
        workers,
        "read_bootstrap_secret",
        lambda *_args, **_kwargs: {"api_key": "user-key"},
    )
    monkeypatch.setattr(
        workers,
        "probe_vm_agent_service_authenticated",
        lambda *_args, **_kwargs: probe_ok,
    )
    monkeypatch.setattr(workers, "publish_desktop_ready", publish)
    monkeypatch.setattr(workers, "record_assistant_session_signal", record_signal)
    return record_signal, publish


def test_desktop_ready_probe_publishes_and_records_when_agent_answers(monkeypatch):
    """A healthy VM whose own push was rejected still reaches readiness.

    The guest only retries /infra/vm/ready ten times and re-arms on a metadata
    change, so without this poll a closed rejection window costs the session its
    desktop permanently.
    """
    record_signal, publish = _desktop_ready_probe_patches(monkeypatch, probe_ok=True)

    workers._run_desktop_ready_probe(
        custom_api=object(),
        core_api=object(),
        namespace="staging",
        assistant_id="1207",
        binding_id="binding-1",
        vm_ref={
            "name": "unity-pool-ubuntu-1-staging",
            "hostname": "vm-1.vm.unify.ai",
            "vmType": "ubuntu",
        },
        secret_name="assistant-session-bootstrap-1207",
    )

    # The runtime learns through Pub/Sub; the CR signal only advances the phase.
    publish.assert_called_once()
    assert publish.call_args.args == ("1207", "vm-1.vm.unify.ai", "ubuntu")
    assert publish.call_args.kwargs["binding_id"] == "binding-1"
    assert publish.call_args.kwargs["desktop_secret"] == "vm-secret-abc"

    assert record_signal.call_args.kwargs["signal_name"] == workers.SIGNAL_DESKTOP_READY
    payload = record_signal.call_args.kwargs["payload"]
    assert payload["state"] == "ready"
    assert payload["hostname"] == "vm-1.vm.unify.ai"
    assert payload["desktopUrl"] == "https://vm-1.vm.unify.ai"
    assert payload["messageId"] == "message-777"
    assert record_signal.call_args.kwargs["source"] == "worker.desktop_ready_probe"


def test_desktop_ready_probe_stays_silent_when_agent_does_not_answer(monkeypatch):
    """An unreachable agent must not be reported ready."""
    record_signal, publish = _desktop_ready_probe_patches(monkeypatch, probe_ok=False)

    workers._run_desktop_ready_probe(
        custom_api=object(),
        core_api=object(),
        namespace="staging",
        assistant_id="1207",
        binding_id="binding-1",
        vm_ref={"name": "unity-pool-ubuntu-1-staging", "hostname": "vm-1.vm.unify.ai"},
        secret_name="assistant-session-bootstrap-1207",
    )

    publish.assert_not_called()
    record_signal.assert_not_called()


def test_desktop_ready_probe_skips_a_superseded_binding(monkeypatch):
    record_signal, publish = _desktop_ready_probe_patches(monkeypatch, probe_ok=True)
    monkeypatch.setattr(
        workers,
        "get_assistant_session",
        lambda *_args, **_kwargs: _session(binding_id="binding-2"),
    )

    workers._run_desktop_ready_probe(
        custom_api=object(),
        core_api=object(),
        namespace="staging",
        assistant_id="1207",
        binding_id="binding-1",
        vm_ref={"name": "unity-pool-ubuntu-1-staging", "hostname": "vm-1.vm.unify.ai"},
        secret_name="assistant-session-bootstrap-1207",
    )

    publish.assert_not_called()
    record_signal.assert_not_called()
