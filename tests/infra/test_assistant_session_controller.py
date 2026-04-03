from copy import deepcopy
import sys
import types
from unittest.mock import MagicMock

fake_kopf = types.SimpleNamespace()


def _identity_decorator(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


fake_kopf.on = types.SimpleNamespace(
    startup=_identity_decorator,
    create=_identity_decorator,
    update=_identity_decorator,
    delete=_identity_decorator,
    probe=_identity_decorator,
)
fake_kopf.timer = _identity_decorator
fake_kopf.OperatorSettings = type("OperatorSettings", (), {})
sys.modules.setdefault("kopf", fake_kopf)

from communication.assistant_session_controller import controller


def _base_session(*, desired_state: str = "Running") -> dict:
    return {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {
            "assistantId": "1207",
            "activationId": "act-1",
            "desiredState": desired_state,
            "desktop": {"required": True, "mode": "ubuntu"},
            "startupSecretRef": "assistant-session-bootstrap-1207",
        },
        "status": {
            "phase": "",
            "observedActivationId": "act-1",
            "conditions": [],
            "bootstrapRetries": 0,
            "vmRetries": 0,
            "desktopProbeFailures": 0,
        },
    }


def _binding(binding_id: str = "binding-1", **overrides) -> dict:
    binding = {"id": binding_id}
    binding.update(overrides)
    return binding


def _job(name: str = "unity-job-1", *, container_ready: bool = True):
    job = MagicMock()
    job.metadata.name = name
    job.metadata.labels = {
        controller.SESSION_REF_LABEL: "assistant-session-1207",
        controller.BINDING_ID_LABEL: "binding-1",
    }
    job.metadata.annotations = {
        controller.SESSION_REF_ANNOTATION: "assistant-session-1207",
        controller.BINDING_ID_ANNOTATION: "binding-1",
        controller.CONTAINER_READY_ANNOTATION: "true" if container_ready else "false",
    }
    job.metadata.deletion_timestamp = None
    job.status.conditions = []
    return job


def test_reconcile_mints_binding_for_unbound_running_session(monkeypatch):
    body = _base_session()
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_batch_api", MagicMock())
    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "PendingJob"
    binding = patch_status.call_args.kwargs["binding"]
    assert binding["id"]
    assert patch_status.call_args.kwargs["observed_activation_id"] == "act-1"


def test_reconcile_records_binding_owned_job(monkeypatch):
    body = _base_session()
    body["status"]["binding"] = _binding("binding-1")
    created_job = _job()
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_batch_api", MagicMock())
    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "_create_bound_job", lambda *_args, **_kwargs: created_job)
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "PendingContainer"
    binding = patch_status.call_args.kwargs["binding"]
    assert binding["jobRef"]["name"] == "unity-job-1"


def test_reconcile_assigns_vm_with_binding_id(monkeypatch):
    body = _base_session()
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
    )
    patch_status = MagicMock()
    assign_pool_vm = MagicMock(
        return_value={"vm_name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
    )

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "_job_for_binding", lambda *_args, **_kwargs: _job())
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "read_bootstrap_secret", lambda *_args, **_kwargs: {"api_key": "key"})
    monkeypatch.setattr(controller, "assign_pool_vm", assign_pool_vm)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assign_pool_vm.assert_called_once_with(
        assistant_id="1207",
        binding_id="binding-1",
        unify_apikey="key",
        vm_type="ubuntu",
    )
    assert patch_status.call_args.kwargs["phase"] == "PendingGuest"
    assert patch_status.call_args.kwargs["binding"]["vmRef"]["name"] == "unity-pool-ubuntu-1"


def test_reconcile_marks_active_from_ready_binding(monkeypatch):
    body = _base_session()
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
        vmAssignedAt="2026-04-03T00:00:05+00:00",
        vmReadyObservedAt="2026-04-03T00:00:10+00:00",
        desktopUrl="https://vm-1.vm.unify.ai",
    )
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "_job_for_binding", lambda *_args, **_kwargs: _job())
    monkeypatch.setattr(
        controller,
        "verify_vm_assignment",
        lambda *_args, **_kwargs: {
            "name": "unity-pool-ubuntu-1",
            "hostname": "vm-1.vm.unify.ai",
        },
    )
    monkeypatch.setattr(controller, "probe_vm_agent_service", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Active"
    assert patch_status.call_args.kwargs["binding"]["desktopUrl"] == "https://vm-1.vm.unify.ai"


def test_reconcile_releases_binding_by_binding_id_when_stopped(monkeypatch):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Active"
    body["status"]["binding"] = _binding(
        "binding-1",
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
    )
    patch_status = MagicMock()
    release_pool_vm = MagicMock(
        return_value={"released": True, "pool_role": "releasing"},
    )

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "_job_for_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "release_pool_vm", release_pool_vm)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    release_pool_vm.assert_called_once_with(
        "1207",
        "binding-1",
        vm_name="unity-pool-ubuntu-1",
    )
    assert patch_status.call_args.kwargs["phase"] == "Releasing"
