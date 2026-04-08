import pytest

from communication.infra.assistant_sessions import (
    binding_desktop_url,
    binding_vm_assignment,
    binding_vm_ref,
    get_phase,
    released_binding,
    session_binding,
    session_signal,
)
from tests.infra.component_harness import AssistantSessionComponentHarness, controller


def _reach_pending_guest(harness: AssistantSessionComponentHarness) -> dict:
    harness.reconcile()
    return harness.reconcile()


def _reach_active(harness: AssistantSessionComponentHarness) -> dict:
    _reach_pending_guest(harness)
    harness.post_vm_ready()
    harness.reconcile()
    return harness.reconcile()


def test_component_desktop_ready_flow_reaches_active(monkeypatch):
    harness = AssistantSessionComponentHarness()
    harness.install(monkeypatch)

    after_queue = harness.reconcile()
    assert get_phase(after_queue) == "PendingVM"
    assert binding_vm_ref(session_binding(after_queue))["name"] == harness.vm_name
    assert binding_vm_assignment(session_binding(after_queue)) == {}

    after_assignment = harness.reconcile()
    assert get_phase(after_assignment) == "PendingGuest"
    assert binding_vm_ref(session_binding(after_assignment))["name"] == harness.vm_name

    ready_response = harness.post_vm_ready()
    assert ready_response.status_code == 200
    assert ready_response.json()["message_id"] == "message-1"
    assert session_signal(harness.session, "desktopReady")["state"] == "ready"

    after_ready = harness.reconcile()
    assert get_phase(after_ready) == "PendingGuest"
    assert (
        binding_desktop_url(session_binding(after_ready))
        == f"https://{harness.vm_hostname}"
    )
    assert session_signal(after_ready, "desktopReady") == {}
    assert session_signal(after_ready, "vmGuestHealth")["state"] == "ready"

    after_guest_probe = harness.reconcile()
    assert get_phase(after_guest_probe) == "Active"
    assert session_signal(after_guest_probe, "vmGuestHealth") == {}
    assert any(
        condition["type"] == "DesktopReady" and condition["status"] == "True"
        for condition in after_guest_probe["status"]["conditions"]
    )


def test_component_vm_ready_rejects_stale_binding(monkeypatch):
    harness = AssistantSessionComponentHarness()
    harness.install(monkeypatch)
    harness.reconcile()
    harness.reconcile()

    response = harness.post_vm_ready(binding_id="stale-binding")

    assert response.status_code == 409
    assert session_signal(harness.session, "desktopReady") == {}


@pytest.mark.parametrize(
    ("assignment_mode", "expected_state", "expected_error"),
    [
        ("capacity", "capacity", "Waiting for VM capacity"),
        ("waiting_release", "waiting_release", "assistant disk still attached"),
        ("error", "error", "RuntimeError: assignment boom"),
    ],
)
def test_component_assignment_signal_variants_keep_pending_vm(
    monkeypatch,
    assignment_mode,
    expected_state,
    expected_error,
):
    harness = AssistantSessionComponentHarness(assignment_mode=assignment_mode)
    harness.install(monkeypatch)

    after_queue = harness.reconcile()
    assert get_phase(after_queue) == "PendingVM"
    assert (
        binding_vm_assignment(session_binding(after_queue))["state"] == expected_state
    )

    after_signal = harness.reconcile()
    assert get_phase(after_signal) == "PendingVM"
    assert after_signal["status"]["lastError"] == expected_error
    assert session_binding(after_signal)["id"] == harness.binding_id
    assert binding_vm_ref(session_binding(after_signal)) == {}


def test_component_vm_ownership_loss_restarts_binding(monkeypatch):
    harness = AssistantSessionComponentHarness()
    harness.install(monkeypatch)

    harness.reconcile()
    pending_guest = harness.reconcile()
    old_binding_id = session_binding(pending_guest)["id"]

    harness.force_assignment_loss = True
    after_loss = harness.reconcile()

    assert get_phase(after_loss) == "PendingJob"
    assert session_binding(after_loss)["id"] != old_binding_id
    assert binding_vm_ref(session_binding(after_loss)) == {}
    assert after_loss["status"]["vmRetries"] == 1
    assert (
        after_loss["status"]["lastError"]
        == "Binding lost VM ownership before desktop became ready"
    )


def test_component_guest_probe_failure_recovers_without_replacing_binding(monkeypatch):
    harness = AssistantSessionComponentHarness(probe_results=[False, True])
    harness.install(monkeypatch)

    harness.reconcile()
    harness.reconcile()
    harness.post_vm_ready()

    after_ready = harness.reconcile()
    binding_id = session_binding(after_ready)["id"]
    assert session_signal(after_ready, "vmGuestHealth")["state"] == "failed"

    after_failure = harness.reconcile()
    assert get_phase(after_failure) == "Active"
    assert after_failure["status"]["desktopProbeFailures"] == 1
    assert session_binding(after_failure)["id"] == binding_id

    after_reprobe = harness.reconcile()
    assert session_signal(after_reprobe, "vmGuestHealth")["state"] == "ready"

    after_recovery = harness.reconcile()
    assert get_phase(after_recovery) == "Active"
    assert after_recovery["status"]["desktopProbeFailures"] == 0
    assert session_binding(after_recovery)["id"] == binding_id


def test_component_repeated_guest_probe_failures_restart_binding(monkeypatch):
    harness = AssistantSessionComponentHarness(
        probe_results=[False] * controller.DESKTOP_LIVENESS_FAILURE_THRESHOLD,
    )
    harness.install(monkeypatch)

    harness.reconcile()
    harness.reconcile()
    harness.post_vm_ready()
    harness.reconcile()

    current = harness.reconcile()
    old_binding_id = session_binding(current)["id"]
    while get_phase(current) == "Active":
        current = harness.reconcile()
        if get_phase(current) == "Active":
            current = harness.reconcile()

    assert get_phase(current) == "Releasing"
    current = harness.reconcile()
    assert session_binding(current)["releaseRequestedAt"]

    response = harness.post_vm_release_complete()
    assert response.status_code == 200
    harness.jobs.pop(harness.job_name, None)
    harness.pods.pop(harness.pod_name, None)
    current = harness.reconcile()

    assert get_phase(current) == "PendingJob"
    assert session_binding(current)["id"] != old_binding_id
    assert current["status"]["vmRetries"] == 1
    assert (
        current["status"]["lastError"]
        == "Desktop VM became unreachable after readiness"
    )


def test_component_release_flow_reaches_released(monkeypatch):
    harness = AssistantSessionComponentHarness()
    harness.install(monkeypatch)

    active = _reach_active(harness)
    assert get_phase(active) == "Active"
    old_binding_id = session_binding(active)["id"]

    harness.session["spec"]["desiredState"] = "Stopped"
    releasing = harness.reconcile()
    assert get_phase(releasing) == "Releasing"
    assert session_signal(releasing, "vmReleaseRequest")["state"] == "requested"

    release_requested = harness.reconcile()
    assert get_phase(release_requested) == "Releasing"
    assert session_binding(release_requested)["releaseRequestedAt"]

    response = harness.post_vm_release_complete()
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert session_signal(harness.session, "vmReleaseComplete")["state"] == "completed"

    harness.jobs.pop(harness.job_name, None)
    harness.pods.pop(harness.pod_name, None)
    released = harness.reconcile()
    assert get_phase(released) == "Released"
    assert released["status"]["binding"] is None
    assert harness.runtime_vm_present is False
    assert released_binding(released, old_binding_id)["releaseCompletedAt"]


def test_component_activation_replacement_waits_for_release_then_mints_new_binding(
    monkeypatch,
):
    harness = AssistantSessionComponentHarness()
    harness.install(monkeypatch)

    active = _reach_active(harness)
    old_binding_id = session_binding(active)["id"]

    harness.session["spec"]["activationId"] = "act-2"

    releasing = harness.reconcile()
    assert get_phase(releasing) == "Releasing"
    assert releasing["status"]["observedActivationId"] == "act-1"
    assert session_binding(releasing)["id"] == old_binding_id
    assert session_signal(releasing, "vmReleaseRequest")["state"] == "requested"

    release_requested = harness.reconcile()
    assert get_phase(release_requested) == "Releasing"
    assert release_requested["status"]["observedActivationId"] == "act-1"
    assert session_binding(release_requested)["releaseRequestedAt"]

    response = harness.post_vm_release_complete()
    assert response.status_code == 200
    harness.jobs.pop(harness.job_name, None)
    harness.pods.pop(harness.pod_name, None)

    restarted = harness.reconcile()
    assert get_phase(restarted) == "PendingJob"
    assert restarted["status"]["observedActivationId"] == "act-2"
    assert session_binding(restarted)["id"] != old_binding_id
    assert restarted["status"]["lastError"] == ""
    assert released_binding(restarted, old_binding_id)["releaseCompletedAt"]


def test_component_job_missing_restarts_after_release_cleanup(monkeypatch):
    harness = AssistantSessionComponentHarness()
    harness.install(monkeypatch)

    pending_guest = _reach_pending_guest(harness)
    old_binding_id = session_binding(pending_guest)["id"]
    harness.jobs.pop(harness.job_name, None)
    harness.pods.pop(harness.pod_name, None)

    releasing = harness.reconcile()
    assert get_phase(releasing) == "Releasing"
    assert session_binding(releasing)["id"] == old_binding_id
    assert session_signal(releasing, "vmReleaseRequest")["state"] == "requested"
    assert (
        releasing["status"]["lastError"]
        == "Recorded binding Job disappeared before runtime became ready"
    )

    release_requested = harness.reconcile()
    assert get_phase(release_requested) == "Releasing"
    assert session_binding(release_requested)["releaseRequestedAt"]

    response = harness.post_vm_release_complete()
    assert response.status_code == 200

    restarted = harness.reconcile()
    assert get_phase(restarted) == "PendingJob"
    assert session_binding(restarted)["id"] != old_binding_id
    assert (
        restarted["status"]["lastError"]
        == "Recorded binding Job disappeared before runtime became ready"
    )
