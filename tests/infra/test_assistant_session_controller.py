from copy import deepcopy
import sys
import types
from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException
import pytest

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


class _TemporaryError(Exception):
    def __init__(self, *args, delay=None):
        super().__init__(*args)
        self.delay = delay


fake_kopf.TemporaryError = _TemporaryError
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


def _job(
    name: str = "unity-job-1",
    *,
    container_ready: bool = True,
    terminal_phase: str | None = None,
):
    job = MagicMock()
    job.metadata.name = name
    job.metadata.labels = {
        controller.SESSION_REF_LABEL: "assistant-session-1207",
        controller.BINDING_ID_LABEL: "binding-1",
    }
    if terminal_phase == "Succeeded":
        job.metadata.labels["unity-status"] = "done"
    job.metadata.annotations = {
        controller.SESSION_REF_ANNOTATION: "assistant-session-1207",
        controller.BINDING_ID_ANNOTATION: "binding-1",
        controller.CONTAINER_READY_ANNOTATION: "true" if container_ready else "false",
    }
    job.metadata.deletion_timestamp = None
    job.status.conditions = []
    if terminal_phase == "Failed":
        job.status.conditions = [types.SimpleNamespace(type="Failed", status="True")]
    elif terminal_phase == "Succeeded":
        job.status.conditions = [types.SimpleNamespace(type="Complete", status="True")]
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
    monkeypatch.setattr(
        controller,
        "_claim_idle_job_for_binding",
        lambda *_args, **_kwargs: created_job,
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "PendingContainer"
    binding = patch_status.call_args.kwargs["binding"]
    assert binding["jobRef"]["name"] == "unity-job-1"


def test_claim_idle_job_for_binding_reuses_existing_job_for_same_binding(monkeypatch):
    binding = _binding("binding-1")
    existing_job = _job(name="unity-job-1", container_ready=False)
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value = MagicMock(items=[existing_job])

    monkeypatch.setattr(controller, "_batch_api", batch_api)

    job = controller._claim_idle_job_for_binding(
        "1207",
        "assistant-session-1207",
        binding,
    )

    assert job is existing_job
    batch_api.patch_namespaced_job.assert_not_called()


def test_claim_idle_job_filters_by_current_image_hash(monkeypatch):
    binding = _binding("binding-1")
    batch_api = MagicMock()
    batch_api.list_namespaced_job.side_effect = [
        MagicMock(items=[]),
        MagicMock(items=[]),
    ]

    monkeypatch.setattr(controller, "_batch_api", batch_api)
    monkeypatch.setattr(controller, "_get_current_image_hash", lambda: "abc123")

    job = controller._claim_idle_job_for_binding(
        "1207",
        "assistant-session-1207",
        binding,
    )

    assert job is None
    selector = batch_api.list_namespaced_job.call_args_list[-1].kwargs["label_selector"]
    assert "unity-image-hash=abc123" in selector


def test_claim_idle_job_skips_hash_filter_when_gcs_unavailable(monkeypatch):
    binding = _binding("binding-1")
    batch_api = MagicMock()
    batch_api.list_namespaced_job.side_effect = [
        MagicMock(items=[]),
        MagicMock(items=[]),
    ]

    monkeypatch.setattr(controller, "_batch_api", batch_api)
    monkeypatch.setattr(controller, "_get_current_image_hash", lambda: None)

    controller._claim_idle_job_for_binding(
        "1207",
        "assistant-session-1207",
        binding,
    )

    selector = batch_api.list_namespaced_job.call_args_list[-1].kwargs["label_selector"]
    assert selector == "app=unity,unity-status=idle"
    assert "unity-image-hash" not in selector


def test_claim_idle_job_prefers_newest_job(monkeypatch):
    binding = _binding("binding-1")
    old_job = _job(name="unity-2026-01-01-00-00-00-aaa", container_ready=False)
    old_job.status.active = 1
    old_job.metadata.deletion_timestamp = None
    new_job = _job(name="unity-2026-04-05-12-00-00-bbb", container_ready=False)
    new_job.status.active = 1
    new_job.metadata.deletion_timestamp = None
    batch_api = MagicMock()
    batch_api.list_namespaced_job.side_effect = [
        MagicMock(items=[]),
        MagicMock(items=[old_job, new_job]),
    ]
    batch_api.read_namespaced_job.return_value = new_job

    monkeypatch.setattr(controller, "_batch_api", batch_api)
    monkeypatch.setattr(controller, "_get_current_image_hash", lambda: None)

    job = controller._claim_idle_job_for_binding(
        "1207",
        "assistant-session-1207",
        binding,
    )

    assert job is new_job
    patched_name = batch_api.patch_namespaced_job.call_args.kwargs["name"]
    assert patched_name == "unity-2026-04-05-12-00-00-bbb"


def test_job_for_binding_lists_by_binding_when_jobref_missing(monkeypatch):
    binding = _binding("binding-1")
    existing_job = _job(name="unity-job-1", container_ready=False)
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value = MagicMock(items=[existing_job])

    monkeypatch.setattr(controller, "_batch_api", batch_api)

    job = controller._job_for_binding("assistant-session-1207", binding)

    assert job is existing_job
    batch_api.read_namespaced_job.assert_not_called()
    batch_api.list_namespaced_job.assert_called_once()


def test_job_for_binding_does_not_rediscover_when_named_job_is_missing(monkeypatch):
    binding = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
    )
    existing_job = _job(name="unity-job-2", container_ready=False)
    batch_api = MagicMock()
    batch_api.read_namespaced_job.side_effect = ApiException(status=404)
    batch_api.list_namespaced_job.return_value = MagicMock(items=[existing_job])

    monkeypatch.setattr(controller, "_batch_api", batch_api)

    job = controller._job_for_binding("assistant-session-1207", binding)

    assert job is None
    batch_api.read_namespaced_job.assert_called_once()
    batch_api.list_namespaced_job.assert_not_called()


def test_job_for_binding_does_not_rediscover_when_named_job_mismatches(monkeypatch):
    binding = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
    )
    mismatched_job = _job(name="unity-job-1", container_ready=False)
    mismatched_job.metadata.labels[controller.BINDING_ID_LABEL] = "binding-other"
    batch_api = MagicMock()
    batch_api.read_namespaced_job.return_value = mismatched_job
    batch_api.list_namespaced_job.return_value = MagicMock(
        items=[_job(name="unity-job-2")],
    )

    monkeypatch.setattr(controller, "_batch_api", batch_api)

    job = controller._job_for_binding("assistant-session-1207", binding)

    assert job is None
    batch_api.read_namespaced_job.assert_called_once()
    batch_api.list_namespaced_job.assert_not_called()


def test_reconcile_waits_for_idle_capacity_when_no_idle_job_available(monkeypatch):
    body = _base_session()
    body["status"]["binding"] = _binding("binding-1")
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_batch_api", MagicMock())
    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "_claim_idle_job_for_binding",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "PendingJob"
    assert patch_status.call_args.kwargs["last_error"] == ""


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
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        lambda *_args, **_kwargs: _job(),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "read_bootstrap_secret",
        lambda *_args, **_kwargs: {"api_key": "key"},
    )
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
    assert (
        patch_status.call_args.kwargs["binding"]["vmRef"]["name"]
        == "unity-pool-ubuntu-1"
    )


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
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        lambda *_args, **_kwargs: _job(),
    )
    monkeypatch.setattr(
        controller,
        "verify_vm_assignment",
        lambda *_args, **_kwargs: {
            "name": "unity-pool-ubuntu-1",
            "hostname": "vm-1.vm.unify.ai",
        },
    )
    monkeypatch.setattr(
        controller,
        "probe_vm_agent_service",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Active"
    assert (
        patch_status.call_args.kwargs["binding"]["desktopUrl"]
        == "https://vm-1.vm.unify.ai"
    )


def test_reconcile_marks_active_without_desktop_when_container_is_ready(monkeypatch):
    body = _base_session()
    body["spec"]["desktop"] = {"required": False, "mode": "macos"}
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
    )
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        lambda *_args, **_kwargs: _job(),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Active"
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"
    assert "vmRef" not in patch_status.call_args.kwargs["binding"]


def test_reconcile_restarts_binding_after_bootstrap_timeout(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingContainer"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        createdAt="2026-04-03T00:00:00+00:00",
    )
    patch_status = MagicMock()
    suspend_job = MagicMock()

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        lambda *_args, **_kwargs: _job(container_ready=False),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "_binding_deadline_exceeded",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(controller, "_suspend_bound_job", suspend_job)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    suspend_job.assert_called_once()
    assert suspend_job.call_args.kwargs["source"] == "controller.bootstrap_timeout"
    assert patch_status.call_args.kwargs["phase"] == "PendingJob"
    assert patch_status.call_args.kwargs["binding"]["id"] != "binding-1"
    assert patch_status.call_args.kwargs["bootstrap_retries"] == 1
    assert (
        "Container did not become ready within"
        in patch_status.call_args.kwargs["last_error"]
    )


def test_reconcile_restarts_after_terminal_job_cleanup(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingContainer"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
    )
    patch_status = MagicMock()
    terminal_job = _job(terminal_phase="Failed")

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        MagicMock(side_effect=[terminal_job, terminal_job, None]),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: ([], []),
    )
    monkeypatch.setattr(controller, "find_vm_with_disk", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "PendingJob"
    assert patch_status.call_args.kwargs["binding"]["id"] != "binding-1"
    assert patch_status.call_args.kwargs["last_error"] == (
        "Job reached terminal phase Failed"
    )


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
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: (
            [
                {
                    "assistant_id": "1207",
                    "pool_role": "assigned",
                    "vm_name": "unity-pool-ubuntu-1",
                },
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        controller,
        "find_vm_with_disk",
        lambda *_args, **_kwargs: "unity-pool-ubuntu-1",
    )
    monkeypatch.setattr(controller, "release_pool_vm", release_pool_vm)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    release_pool_vm.assert_called_once_with(
        "1207",
        "binding-1",
        vm_name="unity-pool-ubuntu-1",
    )
    assert patch_status.call_args.kwargs["phase"] == "Releasing"


def test_reconcile_releasing_binding_still_requests_vm_release_after_vm_ref_appears(
    monkeypatch,
):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        releaseRequestedAt="2026-04-03T00:00:30+00:00",
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
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: (
            [
                {
                    "assistant_id": "1207",
                    "pool_role": "assigned",
                    "vm_name": "unity-pool-ubuntu-1",
                },
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        controller,
        "find_vm_with_disk",
        lambda *_args, **_kwargs: "unity-pool-ubuntu-1",
    )
    monkeypatch.setattr(controller, "release_pool_vm", release_pool_vm)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    release_pool_vm.assert_called_once_with(
        "1207",
        "binding-1",
        vm_name="unity-pool-ubuntu-1",
    )
    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    assert patch_status.call_args.kwargs["binding"]["releaseRequestedAt"] == (
        "2026-04-03T00:00:30+00:00"
    )


def test_reconcile_finishes_release_when_runtime_artifacts_are_already_gone(
    monkeypatch,
):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        releaseRequestedAt="2026-04-03T00:00:30+00:00",
    )
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "_job_for_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: ([], []),
    )
    monkeypatch.setattr(controller, "find_vm_with_disk", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Released"
    assert patch_status.call_args.kwargs["binding"] is None


def test_reconcile_releases_failed_stopped_session_without_binding(monkeypatch):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Failed"
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Released"
    assert patch_status.call_args.kwargs["binding"] is None


def test_reconcile_treats_terminating_session_as_stopped(monkeypatch):
    body = _base_session()
    body["metadata"]["deletionTimestamp"] = "2026-04-05T15:39:56Z"
    body["status"]["binding"] = _binding(
        "binding-1",
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
    )
    patch_status = MagicMock()
    release_state = MagicMock(
        return_value=("Releasing", body["status"]["binding"], [], ""),
    )

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "_binding_release_state", release_state)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert release_state.call_args.kwargs["source_reason"] == "desired_stop"
    assert patch_status.call_args.kwargs["phase"] == "Releasing"


def test_reconcile_job_missing_keeps_releasing_until_vm_cleanup_finishes(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingGuest"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
        vmAssignedAt="2026-04-03T00:00:05+00:00",
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
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: (
            [
                {
                    "assistant_id": "1207",
                    "pool_role": "assigned",
                    "vm_name": "unity-pool-ubuntu-1",
                },
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        controller,
        "find_vm_with_disk",
        lambda *_args, **_kwargs: "unity-pool-ubuntu-1",
    )
    monkeypatch.setattr(controller, "release_pool_vm", release_pool_vm)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    release_pool_vm.assert_called_once_with(
        "1207",
        "binding-1",
        vm_name="unity-pool-ubuntu-1",
    )
    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"
    assert "Job disappeared" in patch_status.call_args.kwargs["last_error"]


def test_reconcile_job_missing_restarts_only_after_cleanup_finishes(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingContainer"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
    )
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "_job_for_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: ([], []),
    )
    monkeypatch.setattr(controller, "find_vm_with_disk", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "PendingJob"
    assert patch_status.call_args.kwargs["binding"]["id"] != "binding-1"
    assert patch_status.call_args.kwargs["last_error"] == (
        "Recorded binding Job disappeared before runtime became ready"
    )


def test_reconcile_waits_for_vm_capacity_when_assignment_fails(monkeypatch):
    body = _base_session()
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
    )
    patch_status = MagicMock()
    replenish_pool = MagicMock()
    assign_pool_vm = MagicMock(side_effect=ValueError("Waiting for VM capacity"))

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        lambda *_args, **_kwargs: _job(),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "read_bootstrap_secret",
        lambda *_args, **_kwargs: {"api_key": "key"},
    )
    monkeypatch.setattr(controller, "assign_pool_vm", assign_pool_vm)
    monkeypatch.setattr(controller, "replenish_pool", replenish_pool)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    replenish_pool.assert_called_once_with("ubuntu")
    assert patch_status.call_args.kwargs["phase"] == "PendingVM"
    assert patch_status.call_args.kwargs["last_error"] == "Waiting for VM capacity"
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"


def test_reconcile_waits_for_disk_release_before_assigning_vm(monkeypatch):
    body = _base_session()
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
    )
    patch_status = MagicMock()
    assign_pool_vm = MagicMock(
        side_effect=controller.AssistantDiskInUseError("assistant disk still attached"),
    )

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        lambda *_args, **_kwargs: _job(),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "read_bootstrap_secret",
        lambda *_args, **_kwargs: {"api_key": "key"},
    )
    monkeypatch.setattr(controller, "assign_pool_vm", assign_pool_vm)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "PendingVM"
    assert (
        patch_status.call_args.kwargs["last_error"] == "assistant disk still attached"
    )
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"


def test_reconcile_restarts_after_vm_ownership_loss(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingGuest"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
        vmAssignedAt="2026-04-03T00:00:05+00:00",
    )
    patch_status = MagicMock()
    suspend_job = MagicMock()

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        lambda *_args, **_kwargs: _job(),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "verify_vm_assignment",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(controller, "_suspend_bound_job", suspend_job)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    suspend_job.assert_called_once()
    assert suspend_job.call_args.kwargs["source"] == "controller.vm_ownership_lost"
    assert patch_status.call_args.kwargs["phase"] == "PendingJob"
    assert patch_status.call_args.kwargs["binding"]["id"] != "binding-1"
    assert patch_status.call_args.kwargs["vm_retries"] == 1
    assert patch_status.call_args.kwargs["last_error"] == (
        "Binding lost VM ownership before desktop became ready"
    )


def test_reconcile_restarts_after_vm_readiness_timeout(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingGuest"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
        vmAssignedAt="2026-04-03T00:00:05+00:00",
    )
    patch_status = MagicMock()
    release_state = MagicMock(return_value=("Released", None, [], ""))

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        lambda *_args, **_kwargs: _job(),
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "verify_vm_assignment",
        lambda *_args, **_kwargs: {
            "name": "unity-pool-ubuntu-1",
            "hostname": "vm-1.vm.unify.ai",
        },
    )
    monkeypatch.setattr(
        controller,
        "_binding_deadline_exceeded",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(controller, "_binding_release_state", release_state)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert release_state.call_args.kwargs["source_reason"] == "vm_readiness_timeout"
    assert patch_status.call_args.kwargs["phase"] == "PendingJob"
    assert patch_status.call_args.kwargs["binding"]["id"] != "binding-1"
    assert patch_status.call_args.kwargs["vm_retries"] == 1
    assert (
        "VM did not become ready within" in patch_status.call_args.kwargs["last_error"]
    )


def test_reconcile_transient_desktop_liveness_failure_keeps_binding_active(
    monkeypatch,
):
    body = _base_session()
    body["status"]["phase"] = "Active"
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
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        lambda *_args, **_kwargs: _job(),
    )
    monkeypatch.setattr(
        controller,
        "verify_vm_assignment",
        lambda *_args, **_kwargs: {
            "name": "unity-pool-ubuntu-1",
            "hostname": "vm-1.vm.unify.ai",
        },
    )
    monkeypatch.setattr(
        controller,
        "probe_vm_agent_service",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Active"
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"
    assert patch_status.call_args.kwargs["desktop_probe_failures"] == 1


def test_reconcile_restarts_after_desktop_liveness_failure(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "Active"
    body["status"]["desktopProbeFailures"] = (
        controller.DESKTOP_LIVENESS_FAILURE_THRESHOLD - 1
    )
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
    release_state = MagicMock(return_value=("Released", None, [], ""))

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "_job_for_binding",
        lambda *_args, **_kwargs: _job(),
    )
    monkeypatch.setattr(
        controller,
        "verify_vm_assignment",
        lambda *_args, **_kwargs: {
            "name": "unity-pool-ubuntu-1",
            "hostname": "vm-1.vm.unify.ai",
        },
    )
    monkeypatch.setattr(
        controller,
        "probe_vm_agent_service",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(controller, "_binding_release_state", release_state)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert release_state.call_args.kwargs["source_reason"] == "desktop_liveness_failed"
    assert patch_status.call_args.kwargs["phase"] == "PendingJob"
    assert patch_status.call_args.kwargs["binding"]["id"] != "binding-1"
    assert patch_status.call_args.kwargs["vm_retries"] == 1
    assert patch_status.call_args.kwargs["last_error"] == (
        "Desktop VM became unreachable after readiness"
    )


def test_reconcile_activation_replacement_waits_for_release(monkeypatch):
    body = _base_session()
    body["spec"]["activationId"] = "act-2"
    body["status"]["phase"] = "Active"
    body["status"]["observedActivationId"] = "act-1"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
        vmAssignedAt="2026-04-03T00:00:05+00:00",
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
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: (
            [
                {
                    "assistant_id": "1207",
                    "pool_role": "assigned",
                    "vm_name": "unity-pool-ubuntu-1",
                },
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        controller,
        "find_vm_with_disk",
        lambda *_args, **_kwargs: "unity-pool-ubuntu-1",
    )
    monkeypatch.setattr(controller, "release_pool_vm", release_pool_vm)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    release_pool_vm.assert_called_once_with(
        "1207",
        "binding-1",
        vm_name="unity-pool-ubuntu-1",
    )
    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    assert patch_status.call_args.kwargs["observed_activation_id"] == "act-1"
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"


def test_reconcile_does_not_mark_released_while_disk_is_still_attached(monkeypatch):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        releaseRequestedAt="2026-04-03T00:00:30+00:00",
    )
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "_job_for_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: ([], []),
    )
    monkeypatch.setattr(
        controller,
        "find_vm_with_disk",
        lambda *_args, **_kwargs: "unity-pool-ubuntu-1",
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"


def test_reconcile_does_not_mark_released_while_other_assistant_vm_exists(monkeypatch):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        releaseRequestedAt="2026-04-03T00:00:30+00:00",
    )
    patch_status = MagicMock()

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "_job_for_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: (
            [],
            [
                {
                    "assistant_id": "1207",
                    "binding_id": "binding-other",
                    "pool_role": "assigned",
                    "vm_name": "unity-pool-ubuntu-2",
                },
            ],
        ),
    )
    monkeypatch.setattr(
        controller,
        "find_vm_with_disk",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"


def test_delete_handler_waits_for_runtime_cleanup_before_finalizing(monkeypatch):
    body = _base_session()
    body["metadata"]["deletionTimestamp"] = "2026-04-05T15:39:56Z"
    update_status = MagicMock()
    core_api = MagicMock()

    snapshots = [deepcopy(body), deepcopy(body)]
    monkeypatch.setattr(controller, "_core_api", core_api)
    monkeypatch.setattr(
        controller,
        "_refresh_session_snapshot",
        lambda *_args: snapshots.pop(0),
    )
    monkeypatch.setattr(controller, "_update_status_for_session", update_status)
    monkeypatch.setattr(
        controller,
        "_session_delete_cleanup_complete",
        lambda *_args: False,
    )

    with pytest.raises(controller.kopf.TemporaryError):
        controller.delete_session(deepcopy(body))

    update_status.assert_called_once()
    core_api.delete_namespaced_secret.assert_not_called()


def test_delete_handler_deletes_secret_after_runtime_cleanup_completes(monkeypatch):
    body = _base_session()
    body["metadata"]["deletionTimestamp"] = "2026-04-05T15:39:56Z"
    update_status = MagicMock()
    core_api = MagicMock()

    snapshots = [deepcopy(body), deepcopy(body)]
    monkeypatch.setattr(controller, "_core_api", core_api)
    monkeypatch.setattr(
        controller,
        "_refresh_session_snapshot",
        lambda *_args: snapshots.pop(0),
    )
    monkeypatch.setattr(controller, "_update_status_for_session", update_status)
    monkeypatch.setattr(
        controller,
        "_session_delete_cleanup_complete",
        lambda *_args: True,
    )

    controller.delete_session(deepcopy(body))

    update_status.assert_called_once()
    core_api.delete_namespaced_secret.assert_called_once_with(
        name="assistant-session-bootstrap-1207",
        namespace=controller.WATCH_NAMESPACE,
    )
