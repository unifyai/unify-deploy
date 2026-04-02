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
from communication.infra.assistant_sessions import build_condition


def _make_bound_job(name: str = "unity-job-1", *, container_ready: bool = True):
    job = MagicMock()
    job.metadata.name = name
    job.metadata.labels = {}
    job.metadata.annotations = {
        controller.CONTAINER_READY_ANNOTATION: "true" if container_ready else "false",
    }
    job.metadata.deletion_timestamp = None
    job.status.conditions = []
    return job


def test_new_activation_resets_stale_vm_retry_budget(monkeypatch):
    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "_ensure_job_binding",
        lambda *_args: _make_bound_job(),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args: None)
    monkeypatch.setattr(
        controller,
        "read_bootstrap_secret",
        lambda *_args: {"api_key": "key"},
    )

    assign_pool_vm = MagicMock(
        return_value={"vm_name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
    )
    replenish_pool = MagicMock()
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "assign_pool_vm", assign_pool_vm)
    monkeypatch.setattr(controller, "replenish_pool", replenish_pool)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    body = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {
            "assistantId": "1207",
            "activationId": "act-new",
            "desktopRequired": True,
            "desktopMode": "ubuntu",
            "startupSecretRef": "assistant-session-bootstrap-1207",
        },
        "status": {
            "observedActivationId": "act-old",
            "vmRetries": 99,
            "conditions": [
                build_condition(
                    "VMAssigned",
                    False,
                    "RetriesExhausted",
                    "stale retry budget",
                ),
            ],
        },
    }
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )

    controller._update_status_for_session(body)

    assign_pool_vm.assert_called_once()
    replenish_pool.assert_not_called()
    patch_status.assert_called_once()
    assert patch_status.call_args.kwargs["phase"] == "PendingVM"
    assert patch_status.call_args.kwargs["bootstrap_retries"] == 0
    assert patch_status.call_args.kwargs["vm_retries"] == 0
    assert patch_status.call_args.kwargs["desktop_probe_failures"] == 0


def test_reconcile_refreshes_latest_session_before_acting(monkeypatch):
    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "_ensure_job_binding",
        lambda *_args: _make_bound_job(),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args: None)
    monkeypatch.setattr(
        controller,
        "read_bootstrap_secret",
        lambda *_args: {"api_key": "key"},
    )

    latest_session = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {
            "assistantId": "1207",
            "activationId": "act-new",
            "desktopRequired": True,
            "desktopMode": "ubuntu",
            "startupSecretRef": "assistant-session-bootstrap-1207",
        },
        "status": {
            "observedActivationId": "act-old",
            "vmRetries": 99,
            "conditions": [
                build_condition(
                    "VMAssigned",
                    False,
                    "RetriesExhausted",
                    "stale retry budget",
                ),
            ],
        },
    }

    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(latest_session),
    )

    assign_pool_vm = MagicMock(
        return_value={"vm_name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
    )
    replenish_pool = MagicMock()
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "assign_pool_vm", assign_pool_vm)
    monkeypatch.setattr(controller, "replenish_pool", replenish_pool)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    stale_body = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {
            "assistantId": "1207",
            "activationId": "act-old",
            "desktopRequired": True,
            "desktopMode": "ubuntu",
            "startupSecretRef": "assistant-session-bootstrap-1207",
        },
        "status": {
            "observedActivationId": "act-old",
            "vmRetries": 99,
            "conditions": [
                build_condition(
                    "VMAssigned",
                    False,
                    "RetriesExhausted",
                    "stale retry budget",
                ),
            ],
        },
    }

    controller._update_status_for_session(stale_body)

    assign_pool_vm.assert_called_once()
    replenish_pool.assert_not_called()
    assert patch_status.call_args.kwargs["observed_activation_id"] == "act-new"
    assert patch_status.call_args.kwargs["bootstrap_retries"] == 0
    assert patch_status.call_args.kwargs["vm_retries"] == 0
    assert patch_status.call_args.kwargs["desktop_probe_failures"] == 0


def test_vm_reassignment_refreshes_vm_assigned_transition_time(monkeypatch):
    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "_ensure_job_binding",
        lambda *_args: _make_bound_job(),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args: None)

    old_transition_time = "2026-04-01T00:00:00+00:00"
    body = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {
            "assistantId": "1207",
            "activationId": "act-1",
            "desktopRequired": True,
            "desktopMode": "ubuntu",
            "startupSecretRef": "assistant-session-bootstrap-1207",
        },
        "status": {
            "observedActivationId": "act-1",
            "jobRef": {"name": "unity-job-1"},
            "vmRef": {
                "name": "unity-pool-ubuntu-1",
                "hostname": "vm-1.vm.unify.ai",
            },
            "conditions": [
                build_condition(
                    "ContainerAssigned",
                    True,
                    "Bound",
                    "Session job bound",
                ),
                build_condition(
                    "ContainerReady",
                    True,
                    "UnityReady",
                    "Unity bootstrap complete",
                ),
                {
                    "type": "VMAssigned",
                    "status": "True",
                    "reason": "Assigned",
                    "message": "Managed VM assigned",
                    "lastTransitionTime": old_transition_time,
                },
                build_condition(
                    "DesktopReady",
                    False,
                    "WaitingForDesktop",
                    "Waiting for authenticated desktop readiness",
                ),
                build_condition(
                    "Active",
                    False,
                    "WaitingForDesktop",
                    "Desktop session not ready yet",
                ),
            ],
        },
    }

    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "verify_vm_assignment",
        lambda *_args, **_kwargs: {
            "name": "unity-pool-ubuntu-2",
            "hostname": "vm-2.vm.unify.ai",
        },
    )

    release_pool_vm = MagicMock()
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "release_pool_vm", release_pool_vm)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    release_pool_vm.assert_not_called()
    assert patch_status.call_args.kwargs["phase"] == "PendingVM"
    assert patch_status.call_args.kwargs["vm_ref"]["name"] == "unity-pool-ubuntu-2"
    condition_map = {
        condition["type"]: condition
        for condition in patch_status.call_args.kwargs["conditions"]
    }
    assert condition_map["VMAssigned"]["reason"] == "Assigned"
    assert condition_map["VMAssigned"]["lastTransitionTime"] != old_transition_time


def test_bootstrap_timeout_skips_stale_unbind_when_session_moved_on(monkeypatch):
    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "_ensure_job_binding",
        lambda *_args: _make_bound_job(container_ready=False),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args: None)

    current_session = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {
            "assistantId": "1207",
            "activationId": "act-1",
            "desktopRequired": True,
            "desktopMode": "ubuntu",
            "startupSecretRef": "assistant-session-bootstrap-1207",
        },
        "status": {
            "observedActivationId": "act-1",
            "jobRef": {"name": "unity-job-1"},
            "conditions": [
                {
                    "type": "ContainerReady",
                    "status": "False",
                    "reason": "WaitingForUnity",
                    "message": "Unity has not yet signaled container-ready",
                    "lastTransitionTime": "2026-04-01T00:00:00+00:00",
                },
            ],
        },
    }
    moved_session = deepcopy(current_session)
    moved_session["status"]["jobRef"] = {"name": "unity-job-2"}

    session_reads = iter([current_session, moved_session])
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(next(session_reads)),
    )

    unbind_job = MagicMock()
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_unbind_job", unbind_job)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(current_session))

    unbind_job.assert_not_called()
    patch_status.assert_not_called()


def test_vm_timeout_skips_stale_release_when_session_moved_on(monkeypatch):
    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "_ensure_job_binding",
        lambda *_args: _make_bound_job(),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args: None)

    current_session = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {
            "assistantId": "1207",
            "activationId": "act-1",
            "desktopRequired": True,
            "desktopMode": "ubuntu",
            "startupSecretRef": "assistant-session-bootstrap-1207",
        },
        "status": {
            "observedActivationId": "act-1",
            "jobRef": {"name": "unity-job-1"},
            "vmRef": {
                "name": "unity-pool-ubuntu-1",
                "hostname": "vm-1.vm.unify.ai",
            },
            "conditions": [
                build_condition(
                    "ContainerAssigned",
                    True,
                    "Bound",
                    "Session job bound",
                ),
                build_condition(
                    "ContainerReady",
                    True,
                    "UnityReady",
                    "Unity bootstrap complete",
                ),
                {
                    "type": "VMAssigned",
                    "status": "True",
                    "reason": "Assigned",
                    "message": "Managed VM assigned",
                    "lastTransitionTime": "2026-04-01T00:00:00+00:00",
                },
                build_condition(
                    "DesktopReady",
                    False,
                    "WaitingForDesktop",
                    "Waiting for authenticated desktop readiness",
                ),
            ],
        },
    }
    moved_session = deepcopy(current_session)
    moved_session["status"]["jobRef"] = {"name": "unity-job-2"}
    moved_session["status"]["vmRef"] = {
        "name": "unity-pool-ubuntu-2",
        "hostname": "vm-2.vm.unify.ai",
    }

    session_reads = iter([current_session, moved_session])
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(next(session_reads)),
    )
    monkeypatch.setattr(
        controller,
        "verify_vm_assignment",
        lambda *_args, **_kwargs: deepcopy(current_session["status"]["vmRef"]),
    )

    release_pool_vm = MagicMock()
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "release_pool_vm", release_pool_vm)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(current_session))

    release_pool_vm.assert_not_called()
    patch_status.assert_not_called()
