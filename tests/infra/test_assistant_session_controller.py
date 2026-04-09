from copy import deepcopy
from datetime import datetime
import sys
import types
from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException
import pytest

from communication.infra.assistant_sessions import (
    build_binding_signal,
    build_binding_vm_assignment,
)
from communication.infra.observability import (
    build_causal_context,
    causal_signal_payload,
    current_causal_context,
)

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


@pytest.fixture(autouse=True)
def _controller_runtime_defaults(monkeypatch):
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value.items = []
    monkeypatch.setattr(controller, "_batch_api", batch_api)
    monkeypatch.setattr(controller, "_coord_api", MagicMock())
    monkeypatch.setattr(controller, "schedule_vm_assignment", lambda **_kwargs: True)
    monkeypatch.setattr(
        controller,
        "schedule_guest_health_probe",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        controller,
        "schedule_vm_release_request",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(controller, "worker_runtime_stats", lambda: {"inflight": 0})
    monkeypatch.setattr(
        controller,
        "acquire_assignment_lease",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        controller,
        "release_assignment_lease",
        lambda *_args, **_kwargs: None,
    )
    return batch_api


@pytest.fixture(autouse=True)
def _stub_released_binding_ledger(monkeypatch):
    monkeypatch.setattr(controller, "record_released_binding", MagicMock())


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
    job.status.active = 0 if terminal_phase in {"Failed", "Succeeded"} else 1
    job.status.conditions = []
    if terminal_phase == "Failed":
        job.status.conditions = [types.SimpleNamespace(type="Failed", status="True")]
    elif terminal_phase == "Succeeded":
        job.status.conditions = [types.SimpleNamespace(type="Complete", status="True")]
    return job


def _pod(
    name: str = "unity-pod-1",
    *,
    phase: str = "Running",
    started_at: str = "2026-04-06T00:00:00+00:00",
):
    pod = MagicMock()
    pod.metadata.name = name
    pod.status.phase = phase
    pod.status.start_time = datetime.fromisoformat(started_at)
    pod.status.container_statuses = []
    if phase == "Running":
        pod.status.container_statuses = [
            types.SimpleNamespace(
                state=types.SimpleNamespace(
                    running=types.SimpleNamespace(
                        started_at=datetime.fromisoformat(started_at),
                    ),
                ),
            ),
        ]
    return pod


def test_reconcile_mints_binding_for_unbound_running_session(monkeypatch):
    body = _base_session()
    patch_status = MagicMock()
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value.items = []

    monkeypatch.setattr(controller, "_batch_api", batch_api)
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
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value.items = []

    monkeypatch.setattr(controller, "_batch_api", batch_api)
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


def test_reconcile_preserves_created_at_and_starts_bootstrap_timer_on_claim(
    monkeypatch,
):
    body = _base_session()
    body["status"]["phase"] = "PendingJob"
    body["status"]["binding"] = _binding(
        "binding-1",
        createdAt="2026-04-03T00:00:00+00:00",
    )
    created_job = _job()
    patch_status = MagicMock()
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value.items = []

    monkeypatch.setattr(controller, "_batch_api", batch_api)
    monkeypatch.setattr(controller, "_custom_api", object())
    core_api = MagicMock()
    core_api.read_namespaced_pod.return_value = _pod()
    monkeypatch.setattr(controller, "_core_api", core_api)
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
    monkeypatch.setattr(
        controller,
        "_current_pod_ref",
        lambda *_args, **_kwargs: {"name": "unity-pod-1", "namespace": "preview"},
    )
    monkeypatch.setattr(controller, "_now_iso", lambda: "2026-04-06T00:00:00+00:00")
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "PendingContainer"
    binding = patch_status.call_args.kwargs["binding"]
    assert binding["jobRef"]["name"] == "unity-job-1"
    assert binding["createdAt"] == "2026-04-03T00:00:00+00:00"
    assert binding["containerBootstrapStartedAt"] == "2026-04-06T00:00:00+00:00"


def test_reconcile_defers_job_claim_while_claim_transition_is_busy(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingJob"
    body["status"]["binding"] = _binding("binding-1")
    patch_status = MagicMock()
    claim_job = MagicMock()

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)
    monkeypatch.setattr(
        controller,
        "acquire_assignment_lease",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(controller, "_claim_idle_job_for_binding", claim_job)

    controller._update_status_for_session(deepcopy(body))

    claim_job.assert_not_called()
    patch_status.assert_not_called()


def test_reconcile_suspends_newly_claimed_job_when_jobref_persist_loses(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingJob"
    body["status"]["binding"] = _binding("binding-1")
    claimed_job = _job(name="unity-job-claimed")
    patch_status = MagicMock(
        side_effect=ApiException(status=409, reason="status conflict"),
    )
    suspend_job = MagicMock()
    latest_body = deepcopy(body)
    latest_body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={
            "name": "unity-job-authoritative",
            "namespace": controller.WATCH_NAMESPACE,
        },
    )

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        MagicMock(side_effect=[deepcopy(body), deepcopy(latest_body)]),
    )
    monkeypatch.setattr(controller, "_job_for_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "_claim_idle_job_for_binding",
        lambda *_args, **_kwargs: claimed_job,
    )
    monkeypatch.setattr(controller, "_current_pod_ref", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)
    monkeypatch.setattr(controller, "_suspend_bound_job", suspend_job)

    with pytest.raises(ApiException) as exc_info:
        controller._update_status_for_session(deepcopy(body))

    assert exc_info.value.status == 409
    suspend_job.assert_called_once_with(
        claimed_job,
        source="controller.claim_conflict_cleanup",
    )


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


def test_claim_idle_job_claims_jobs_in_name_order(monkeypatch):
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
    batch_api.read_namespaced_job.return_value = old_job

    monkeypatch.setattr(controller, "_batch_api", batch_api)
    monkeypatch.setattr(controller, "_get_current_image_hash", lambda: None)

    job = controller._claim_idle_job_for_binding(
        "1207",
        "assistant-session-1207",
        binding,
    )

    assert job is old_job
    patched_name = batch_api.patch_namespaced_job.call_args.kwargs["name"]
    assert patched_name == "unity-2026-01-01-00-00-00-aaa"


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
    batch_api = MagicMock()
    batch_api.list_namespaced_job.return_value.items = []

    monkeypatch.setattr(controller, "_batch_api", batch_api)
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


def test_reconcile_queues_vm_assignment_for_binding(monkeypatch):
    body = _base_session()
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
    )
    patch_status = MagicMock()
    queue_vm_assignment = MagicMock(return_value=True)

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
        "claim_binding_vm_assignment_attempt",
        MagicMock(return_value="attempt-1"),
    )
    monkeypatch.setattr(
        controller,
        "schedule_vm_assignment",
        queue_vm_assignment,
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    queue_vm_assignment.assert_called_once_with(
        custom_api=controller._custom_api,
        core_api=controller._core_api,
        namespace=controller.WATCH_NAMESPACE,
        assistant_id="1207",
        binding_id="binding-1",
        attempt_id="attempt-1",
        secret_name="assistant-session-bootstrap-1207",
        vm_type="ubuntu",
    )
    assert patch_status.call_args.kwargs["phase"] == "PendingVM"
    assert "binding" not in patch_status.call_args.kwargs


def test_reconcile_advances_to_pending_guest_when_binding_vm_ref_present(monkeypatch):
    body = _base_session()
    vm_assigned_at = datetime.now().astimezone().isoformat()
    body["status"]["phase"] = "PendingGuest"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        podRef={"name": "unity-pod-1", "namespace": "preview"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
        vmRef={
            "name": "unity-pool-ubuntu-1",
            "hostname": "vm-1.vm.unify.ai",
            "vmType": "ubuntu",
        },
        vmAssignedAt=vm_assigned_at,
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
        "_read_bound_pod",
        lambda *_args, **_kwargs: MagicMock(
            status=MagicMock(start_time=None, container_statuses=[]),
        ),
    )
    monkeypatch.setattr(
        controller,
        "verify_vm_assignment",
        lambda *_args, **_kwargs: {
            "name": "unity-pool-ubuntu-1",
            "hostname": "vm-1.vm.unify.ai",
            "vmType": "ubuntu",
        },
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "PendingGuest"
    assert (
        patch_status.call_args.kwargs["binding"]["vmRef"]["name"]
        == "unity-pool-ubuntu-1"
    )
    assert (
        patch_status.call_args.kwargs["binding"]["guestHandshakeStartedAt"]
        == vm_assigned_at
    )


def test_reconcile_waits_for_inflight_vm_assignment_attempt(monkeypatch):
    body = _base_session()
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
        vmAssignment=build_binding_vm_assignment(
            attempt_id="attempt-1",
            state="in_progress",
        ),
    )
    patch_status = MagicMock()
    queue_vm_assignment = MagicMock()

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
    monkeypatch.setattr(controller, "schedule_vm_assignment", queue_vm_assignment)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    queue_vm_assignment.assert_not_called()
    assert patch_status.call_args.kwargs["phase"] == "PendingVM"
    assert patch_status.call_args.kwargs["last_error"] == ""


def test_reconcile_carries_release_signal_context_into_next_pending_job(monkeypatch):
    body = _base_session()
    signal_context = build_causal_context(
        caller="views.release_complete",
        reason="http_request",
    )
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        releaseRequestedAt="2026-04-03T00:00:15+00:00",
    )
    body["status"]["signals"] = {
        controller.SIGNAL_VM_RELEASE_COMPLETE: build_binding_signal(
            binding_id="binding-1",
            state="completed",
            observed_at="2026-04-03T00:00:20+00:00",
            vmName="unity-pool-ubuntu-1",
            causal=causal_signal_payload(signal_context),
        ),
    }
    captured: dict[str, object] = {}

    def patch_status(*_args, **kwargs):
        captured["context"] = current_causal_context()
        captured["kwargs"] = kwargs

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "_binding_release_state",
        MagicMock(return_value=("Released", None, [], "")),
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert captured["kwargs"]["phase"] == "PendingJob"
    context = captured["context"]
    assert context["caller"] == "controller.reconcile.pending_job"
    assert context["parent_caller"] == "views.release_complete"
    assert context["root_caller"] == "views.release_complete"
    assert context["reason"] == "post_release_signal"


def test_reconcile_emits_pending_job_stage_when_binding_is_minted(monkeypatch):
    body = _base_session()
    patch_status = MagicMock()
    events: list[tuple[str, dict]] = []

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)
    monkeypatch.setattr(
        controller,
        "emit_observability_event",
        lambda event, **fields: events.append((event, fields)),
    )

    controller._update_status_for_session(deepcopy(body))

    assert any(
        event == "controller.pending_job_stage"
        and fields.get("stage") == "mint_binding"
        and fields.get("stage_state") == "completed"
        for event, fields in events
    )


def test_reconcile_emits_pending_container_wait_stage(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingContainer"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        containerBootstrapStartedAt=controller._now_iso(),
    )
    patch_status = MagicMock()
    events: list[tuple[str, dict]] = []

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
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)
    monkeypatch.setattr(
        controller,
        "emit_observability_event",
        lambda event, **fields: events.append((event, fields)),
    )

    controller._update_status_for_session(deepcopy(body))

    assert any(
        event == "controller.pending_container_stage"
        and fields.get("stage") == "container_ready_wait"
        and fields.get("stage_state") == "pending"
        for event, fields in events
    )


def test_reconcile_waits_for_pod_running_before_starting_bootstrap_timer(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingContainer"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
    )
    patch_status = MagicMock()
    deadline_exceeded = MagicMock(return_value=True)
    core_api = MagicMock()
    core_api.read_namespaced_pod.return_value = _pod(phase="Pending")

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", core_api)
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
    monkeypatch.setattr(
        controller,
        "_current_pod_ref",
        lambda *_args, **_kwargs: {"name": "unity-pod-1", "namespace": "preview"},
    )
    monkeypatch.setattr(
        controller,
        "_binding_deadline_exceeded",
        deadline_exceeded,
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    deadline_exceeded.assert_not_called()
    assert patch_status.call_args.kwargs["phase"] == "PendingContainer"
    assert "containerBootstrapStartedAt" not in patch_status.call_args.kwargs["binding"]
    container_ready_condition = next(
        condition
        for condition in patch_status.call_args.kwargs["conditions"]
        if condition["type"] == "ContainerReady"
    )
    assert container_ready_condition["reason"] == "WaitingForPodStart"


def test_reconcile_consumes_desktop_ready_signal_and_queues_guest_probe(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingGuest"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
        vmAssignedAt="2026-04-03T00:00:05+00:00",
    )
    body["status"]["signals"] = {
        controller.SIGNAL_DESKTOP_READY: build_binding_signal(
            binding_id="binding-1",
            observed_at="2026-04-03T00:00:10+00:00",
            state="ready",
            hostname="vm-1.vm.unify.ai",
            desktopUrl="https://vm-1.vm.unify.ai",
            messageId="message-123",
        ),
    }
    patch_status = MagicMock()
    schedule_guest_probe = MagicMock(return_value=True)

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
        "schedule_guest_health_probe",
        schedule_guest_probe,
    )
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    schedule_guest_probe.assert_called_once_with(
        custom_api=controller._custom_api,
        namespace=controller.WATCH_NAMESPACE,
        assistant_id="1207",
        binding_id="binding-1",
        vm_ref={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
    )
    assert patch_status.call_args.kwargs["phase"] == "PendingGuest"
    assert (
        patch_status.call_args.kwargs["binding"]["desktopUrl"]
        == "https://vm-1.vm.unify.ai"
    )
    assert (
        patch_status.call_args.kwargs["binding"]["vmReadyObservedAt"]
        == "2026-04-03T00:00:10+00:00"
    )
    assert patch_status.call_args.kwargs["signals"] == {}


def test_reconcile_marks_active_from_ready_binding(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "PendingGuest"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
        vmAssignedAt="2026-04-03T00:00:05+00:00",
        vmReadyObservedAt="2026-04-03T00:00:10+00:00",
        desktopUrl="https://vm-1.vm.unify.ai",
    )
    body["status"]["signals"] = {
        controller.SIGNAL_VM_GUEST_HEALTH: build_binding_signal(
            binding_id="binding-1",
            state="ready",
            vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        ),
    }
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
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Active"
    assert (
        patch_status.call_args.kwargs["binding"]["desktopUrl"]
        == "https://vm-1.vm.unify.ai"
    )
    assert patch_status.call_args.kwargs["signals"] == {}


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
        containerBootstrapStartedAt="2026-04-03T00:00:00+00:00",
    )
    patch_status = MagicMock()
    deadline_exceeded = MagicMock(return_value=True)
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
    monkeypatch.setattr(controller, "_binding_deadline_exceeded", deadline_exceeded)
    monkeypatch.setattr(controller, "_suspend_bound_job", suspend_job)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert deadline_exceeded.call_args.args[1] == "containerBootstrapStartedAt"
    assert (
        deadline_exceeded.call_args.args[2]
        == controller.CONTAINER_BOOTSTRAP_DEADLINE_SECONDS
    )
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
    queue_vm_release = MagicMock(return_value=True)

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
    monkeypatch.setattr(controller, "schedule_vm_release_request", queue_vm_release)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    queue_vm_release.assert_called_once_with(
        custom_api=controller._custom_api,
        namespace=controller.WATCH_NAMESPACE,
        assistant_id="1207",
        binding_id="binding-1",
        vm_name="unity-pool-ubuntu-1",
        release_generation=1,
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
        releaseRequestedAt=controller._now_iso(),
    )
    patch_status = MagicMock()
    queue_vm_release = MagicMock(return_value=True)

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
    monkeypatch.setattr(controller, "schedule_vm_release_request", queue_vm_release)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    queue_vm_release.assert_called_once_with(
        custom_api=controller._custom_api,
        namespace=controller.WATCH_NAMESPACE,
        assistant_id="1207",
        binding_id="binding-1",
        vm_name="unity-pool-ubuntu-1",
        release_generation=1,
    )
    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    assert patch_status.call_args.kwargs["binding"]["releaseRequestedAt"]
    assert patch_status.call_args.kwargs["binding"]["releaseGeneration"] == 1


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


def test_reconcile_keeps_releasing_while_other_assistant_job_is_still_live(monkeypatch):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        releaseRequestedAt="2026-04-03T00:00:30+00:00",
        releaseCompletedAt="2026-04-03T00:00:45+00:00",
    )
    patch_status = MagicMock()
    stray_job = _job(name="unity-job-2")
    stray_job.metadata.labels["assistant-id"] = "1207"

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
    controller._batch_api.list_namespaced_job.return_value.items = [stray_job]
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"


def test_reconcile_stopped_without_binding_waits_for_assistant_cleanup(monkeypatch):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Released"
    patch_status = MagicMock()
    stray_job = _job(name="unity-job-2")
    stray_job.metadata.labels["assistant-id"] = "1207"

    monkeypatch.setattr(controller, "_custom_api", object())
    monkeypatch.setattr(controller, "_core_api", MagicMock())
    monkeypatch.setattr(
        controller,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(body),
    )
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: ([], []),
    )
    monkeypatch.setattr(controller, "find_vm_with_disk", lambda *_args, **_kwargs: None)
    controller._batch_api.list_namespaced_job.return_value.items = [stray_job]
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert patch_status.call_args.kwargs["phase"] == "Releasing"
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
    queue_vm_release = MagicMock(return_value=True)

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
    monkeypatch.setattr(controller, "schedule_vm_release_request", queue_vm_release)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    queue_vm_release.assert_called_once_with(
        custom_api=controller._custom_api,
        namespace=controller.WATCH_NAMESPACE,
        assistant_id="1207",
        binding_id="binding-1",
        vm_name="unity-pool-ubuntu-1",
        release_generation=1,
    )
    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"
    assert patch_status.call_args.kwargs["binding"]["releaseGeneration"] == 1
    assert "Job disappeared" in patch_status.call_args.kwargs["last_error"]


def test_reconcile_stopped_binding_recovers_vm_ref_from_owned_runtime(monkeypatch):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        releaseRequestedAt=controller._now_iso(),
    )
    patch_status = MagicMock()
    queue_vm_release = MagicMock(return_value=True)

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
                    "binding_id": "binding-1",
                    "pool_role": "assigned",
                    "vm_name": "unity-pool-ubuntu-1",
                    "hostname": "vm-1.vm.unify.ai",
                    "vm_type": "ubuntu",
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
    monkeypatch.setattr(controller, "schedule_vm_release_request", queue_vm_release)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    queue_vm_release.assert_called_once_with(
        custom_api=controller._custom_api,
        namespace=controller.WATCH_NAMESPACE,
        assistant_id="1207",
        binding_id="binding-1",
        vm_name="unity-pool-ubuntu-1",
        release_generation=1,
    )
    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    binding = patch_status.call_args.kwargs["binding"]
    assert binding["id"] == "binding-1"
    assert binding["releaseGeneration"] == 1
    assert binding["vmRef"] == {
        "name": "unity-pool-ubuntu-1",
        "hostname": "vm-1.vm.unify.ai",
        "vmType": "ubuntu",
    }


def test_reconcile_rearms_timed_out_release_request(monkeypatch):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        releaseRequestedAt="2026-04-03T00:00:30+00:00",
        releaseGeneration=1,
    )
    patch_status = MagicMock()
    queue_vm_release = MagicMock(return_value=True)
    recover_release = MagicMock(
        return_value={
            "action": "rearmed",
            "released": True,
            "pool_role": "releasing",
            "release_generation": 2,
        },
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
                    "binding_id": "binding-1",
                    "pool_role": "releasing",
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
    monkeypatch.setattr(
        controller,
        "recover_stuck_pool_vm_release",
        recover_release,
    )
    monkeypatch.setattr(controller, "schedule_vm_release_request", queue_vm_release)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    recover_release.assert_called_once_with(
        "1207",
        "binding-1",
        vm_name="unity-pool-ubuntu-1",
        current_release_generation=1,
        allow_rearm=True,
        retire_reason="controller_desired_stop_release_timeout",
    )
    queue_vm_release.assert_not_called()
    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    binding = patch_status.call_args.kwargs["binding"]
    assert binding["releaseGeneration"] == 2
    assert binding["releaseRequestedAt"] != "2026-04-03T00:00:30+00:00"
    assert "releaseCompletedAt" not in binding


def test_reconcile_retires_release_after_hard_timeout(monkeypatch):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        releaseRequestedAt="2026-04-03T00:00:30+00:00",
        releaseGeneration=2,
    )
    patch_status = MagicMock()
    queue_vm_release = MagicMock(return_value=True)
    recover_release = MagicMock(
        return_value={
            "action": "retired",
            "retired": True,
            "pool_role": "retired",
            "release_generation": 2,
        },
    )
    cleanup_states = [
        (
            [
                {
                    "assistant_id": "1207",
                    "binding_id": "binding-1",
                    "pool_role": "releasing",
                    "vm_name": "unity-pool-ubuntu-1",
                },
            ],
            [],
            "unity-pool-ubuntu-1",
        ),
        ([], [], None),
    ]

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
        "_owned_runtime_cleanup_state",
        lambda *_args, **_kwargs: cleanup_states.pop(0),
    )
    monkeypatch.setattr(
        controller,
        "recover_stuck_pool_vm_release",
        recover_release,
    )
    monkeypatch.setattr(controller, "schedule_vm_release_request", queue_vm_release)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    recover_release.assert_called_once_with(
        "1207",
        "binding-1",
        vm_name="unity-pool-ubuntu-1",
        current_release_generation=2,
        allow_rearm=False,
        retire_reason="controller_desired_stop_release_timeout",
    )
    queue_vm_release.assert_not_called()
    assert patch_status.call_args.kwargs["phase"] == "Released"
    assert patch_status.call_args.kwargs["binding"] is None


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
    body["status"]["binding"]["vmAssignment"] = build_binding_vm_assignment(
        attempt_id="attempt-1",
        state="capacity",
        message="Waiting for VM capacity",
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

    assert patch_status.call_args.kwargs["phase"] == "PendingVM"
    assert patch_status.call_args.kwargs["last_error"] == "Waiting for VM capacity"
    assert "binding" not in patch_status.call_args.kwargs


def test_reconcile_waits_for_disk_release_before_assigning_vm(monkeypatch):
    body = _base_session()
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
    )
    body["status"]["binding"]["vmAssignment"] = build_binding_vm_assignment(
        attempt_id="attempt-1",
        state="waiting_release",
        message="assistant disk still attached",
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

    assert patch_status.call_args.kwargs["phase"] == "PendingVM"
    assert (
        patch_status.call_args.kwargs["last_error"] == "assistant disk still attached"
    )
    assert "binding" not in patch_status.call_args.kwargs


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
        guestHandshakeStartedAt="2026-04-03T00:00:05+00:00",
    )
    patch_status = MagicMock()
    deadline_exceeded = MagicMock(return_value=True)
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
    monkeypatch.setattr(controller, "_binding_deadline_exceeded", deadline_exceeded)
    monkeypatch.setattr(controller, "_binding_release_state", release_state)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert deadline_exceeded.call_args.args[1] == "guestHandshakeStartedAt"
    assert (
        deadline_exceeded.call_args.args[2] == controller.VM_READINESS_DEADLINE_SECONDS
    )
    assert release_state.call_args.kwargs["source_reason"] == "vm_readiness_timeout"
    assert patch_status.call_args.kwargs["phase"] == "PendingJob"
    assert patch_status.call_args.kwargs["binding"]["id"] != "binding-1"
    assert patch_status.call_args.kwargs["vm_retries"] == 1
    assert (
        "VM did not become ready within" in patch_status.call_args.kwargs["last_error"]
    )


def test_reconcile_uses_windows_vm_readiness_timeout(monkeypatch):
    body = _base_session()
    body["spec"]["desktop"]["mode"] = "windows"
    body["status"]["phase"] = "PendingGuest"
    body["status"]["binding"] = _binding(
        "binding-1",
        jobRef={"name": "unity-job-1", "namespace": "preview"},
        vmRef={"name": "unity-pool-windows-1", "hostname": "vm-1.vm.unify.ai"},
        containerReadyAt="2026-04-03T00:00:00+00:00",
        vmAssignedAt="2026-04-03T00:00:05+00:00",
        guestHandshakeStartedAt="2026-04-03T00:00:05+00:00",
    )
    patch_status = MagicMock()
    deadline_exceeded = MagicMock(return_value=True)
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
            "name": "unity-pool-windows-1",
            "hostname": "vm-1.vm.unify.ai",
        },
    )
    monkeypatch.setattr(controller, "_binding_deadline_exceeded", deadline_exceeded)
    monkeypatch.setattr(controller, "_binding_release_state", release_state)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    assert deadline_exceeded.call_args.args[1] == "guestHandshakeStartedAt"
    assert (
        deadline_exceeded.call_args.args[2]
        == controller.WINDOWS_VM_READINESS_DEADLINE_SECONDS
    )
    assert release_state.call_args.kwargs["source_reason"] == "vm_readiness_timeout"
    assert patch_status.call_args.kwargs["phase"] == "PendingJob"
    assert patch_status.call_args.kwargs["binding"]["id"] != "binding-1"
    assert patch_status.call_args.kwargs["vm_retries"] == 1
    assert (
        f"{int(controller.WINDOWS_VM_READINESS_DEADLINE_SECONDS)}s"
        in patch_status.call_args.kwargs["last_error"]
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
    body["status"]["signals"] = {
        controller.SIGNAL_VM_GUEST_HEALTH: build_binding_signal(
            binding_id="binding-1",
            state="failed",
            vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        ),
    }
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
    body["status"]["signals"] = {
        controller.SIGNAL_VM_GUEST_HEALTH: build_binding_signal(
            binding_id="binding-1",
            state="failed",
            vmRef={"name": "unity-pool-ubuntu-1", "hostname": "vm-1.vm.unify.ai"},
        ),
    }
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
    queue_vm_release = MagicMock(return_value=True)

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
    monkeypatch.setattr(controller, "schedule_vm_release_request", queue_vm_release)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    queue_vm_release.assert_called_once_with(
        custom_api=controller._custom_api,
        namespace=controller.WATCH_NAMESPACE,
        assistant_id="1207",
        binding_id="binding-1",
        vm_name="unity-pool-ubuntu-1",
        release_generation=1,
    )
    assert patch_status.call_args.kwargs["phase"] == "Releasing"
    assert patch_status.call_args.kwargs["observed_activation_id"] == "act-1"
    assert patch_status.call_args.kwargs["binding"]["id"] == "binding-1"
    assert patch_status.call_args.kwargs["binding"]["releaseGeneration"] == 1


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


def test_reconcile_finishes_stale_other_binding_release_without_current_vm_ref(
    monkeypatch,
):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        releaseRequestedAt="2026-04-03T00:00:30+00:00",
    )
    patch_status = MagicMock()
    complete_release = MagicMock(
        return_value={
            "vm_name": "unity-pool-ubuntu-2",
            "binding_id": "binding-old",
            "pool_role": "idle",
        },
    )
    cleanup_states = [
        (
            [],
            [
                {
                    "assistant_id": "1207",
                    "binding_id": "binding-old",
                    "pool_role": "releasing",
                    "vm_name": "unity-pool-ubuntu-2",
                },
            ],
            "unity-pool-ubuntu-2",
        ),
        ([], [], None),
        ([], [], None),
    ]

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
        "_owned_runtime_cleanup_state",
        lambda *_args, **_kwargs: cleanup_states.pop(0),
    )
    monkeypatch.setattr(controller, "complete_pool_vm_release", complete_release)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    complete_release.assert_called_once_with("unity-pool-ubuntu-2", "binding-old")
    assert patch_status.call_args.kwargs["phase"] == "Released"
    assert patch_status.call_args.kwargs["binding"] is None


def test_reconcile_finishes_current_binding_release_without_vm_ref_after_signal(
    monkeypatch,
):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        releaseRequestedAt="2026-04-03T00:00:30+00:00",
    )
    body["status"]["signals"] = {
        controller.SIGNAL_VM_RELEASE_COMPLETE: build_binding_signal(
            binding_id="binding-1",
            state="completed",
            observed_at="2026-04-03T00:00:45+00:00",
            vmName="unity-pool-ubuntu-1",
        ),
    }
    patch_status = MagicMock()
    complete_release = MagicMock(
        return_value={
            "vm_name": "unity-pool-ubuntu-1",
            "binding_id": "binding-1",
            "pool_role": "idle",
        },
    )
    cleanup_states = [
        (
            [
                {
                    "assistant_id": "1207",
                    "binding_id": "binding-1",
                    "pool_role": "releasing",
                    "vm_name": "unity-pool-ubuntu-1",
                },
            ],
            [],
            "unity-pool-ubuntu-1",
        ),
        ([], [], None),
    ]

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
        "_owned_runtime_cleanup_state",
        lambda *_args, **_kwargs: cleanup_states.pop(0),
    )
    monkeypatch.setattr(controller, "complete_pool_vm_release", complete_release)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    complete_release.assert_called_once_with("unity-pool-ubuntu-1", "binding-1")
    assert patch_status.call_args.kwargs["phase"] == "Released"
    assert patch_status.call_args.kwargs["binding"] is None


def test_reconcile_retries_current_binding_release_without_vm_ref_after_signal_consumed(
    monkeypatch,
):
    body = _base_session(desired_state="Stopped")
    body["status"]["phase"] = "Releasing"
    body["status"]["binding"] = _binding(
        "binding-1",
        releaseRequestedAt="2026-04-03T00:00:30+00:00",
        releaseCompletedAt="2026-04-03T00:00:45+00:00",
    )
    patch_status = MagicMock()
    complete_release = MagicMock(
        return_value={
            "vm_name": "unity-pool-ubuntu-1",
            "binding_id": "binding-1",
            "pool_role": "idle",
        },
    )
    cleanup_states = [
        (
            [
                {
                    "assistant_id": "1207",
                    "binding_id": "binding-1",
                    "pool_role": "releasing",
                    "vm_name": "unity-pool-ubuntu-1",
                },
            ],
            [],
            "unity-pool-ubuntu-1",
        ),
        ([], [], None),
    ]

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
        "_owned_runtime_cleanup_state",
        lambda *_args, **_kwargs: cleanup_states.pop(0),
    )
    monkeypatch.setattr(controller, "complete_pool_vm_release", complete_release)
    monkeypatch.setattr(controller, "patch_assistant_session_status", patch_status)

    controller._update_status_for_session(deepcopy(body))

    complete_release.assert_called_once_with("unity-pool-ubuntu-1", "binding-1")
    assert patch_status.call_args.kwargs["phase"] == "Released"
    assert patch_status.call_args.kwargs["binding"] is None


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


def test_session_delete_cleanup_complete_requires_no_assistant_live_jobs(monkeypatch):
    body = _base_session()
    body["status"]["phase"] = "Released"
    stray_job = _job(name="unity-job-2")
    stray_job.metadata.labels["assistant-id"] = "1207"

    monkeypatch.setattr(controller, "_job_for_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        controller,
        "split_binding_runtime_vms",
        lambda *_args, **_kwargs: ([], []),
    )
    monkeypatch.setattr(controller, "find_vm_with_disk", lambda *_args, **_kwargs: None)
    controller._batch_api.list_namespaced_job.return_value.items = [stray_job]

    assert controller._session_delete_cleanup_complete(deepcopy(body)) is False


def test_delete_handler_deletes_secret_after_runtime_cleanup_completes(monkeypatch):
    body = _base_session()
    body["metadata"]["deletionTimestamp"] = "2026-04-05T15:39:56Z"
    update_status = MagicMock()
    core_api = MagicMock()
    core_api.read_namespaced_secret.return_value = types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name="assistant-session-bootstrap-1207",
            annotations={
                "assistantsession.unify.ai/name": "assistant-session-1207",
                "assistantsession.unify.ai/activation-id": "act-1",
            },
        ),
    )

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


def test_delete_handler_skips_secret_when_owner_activation_changed(monkeypatch):
    body = _base_session()
    body["metadata"]["deletionTimestamp"] = "2026-04-05T15:39:56Z"
    update_status = MagicMock()
    core_api = MagicMock()
    core_api.read_namespaced_secret.return_value = types.SimpleNamespace(
        metadata=types.SimpleNamespace(
            name="assistant-session-bootstrap-1207",
            annotations={
                "assistantsession.unify.ai/name": "assistant-session-1207",
                "assistantsession.unify.ai/activation-id": "act-2",
            },
        ),
    )

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
    core_api.delete_namespaced_secret.assert_not_called()
