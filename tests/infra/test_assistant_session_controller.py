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


def _make_bound_job():
    job = MagicMock()
    job.metadata.name = "unity-job-1"
    job.metadata.labels = {}
    job.metadata.annotations = {controller.CONTAINER_READY_ANNOTATION: "true"}
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

    controller._update_status_for_session(body)

    assign_pool_vm.assert_called_once()
    replenish_pool.assert_not_called()
    patch_status.assert_called_once()
    assert patch_status.call_args.kwargs["phase"] == "PendingVM"
    assert patch_status.call_args.kwargs["bootstrap_retries"] == 0
    assert patch_status.call_args.kwargs["vm_retries"] == 0
    assert patch_status.call_args.kwargs["desktop_probe_failures"] == 0
