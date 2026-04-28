import base64
from copy import deepcopy
import json
import logging
from pathlib import Path

import pytest
import yaml

from communication.infra import assistant_sessions as assistant_sessions_module
from communication.infra.assistant_sessions import (
    ACTIVATION_ID_ANNOTATION,
    AssistantSessionTerminatingError,
    SESSION_REF_ANNOTATION,
    SUSPEND_INTENT_REPLACE,
    SUSPEND_INTENT_STOP,
    assistant_session_desired_state,
    assistant_session_is_terminating,
    assistant_session_name,
    assistant_session_observability_fields,
    assistant_session_secret_name,
    build_binding,
    build_assistant_session_spec,
    build_binding_vm_assignment,
    build_condition,
    build_binding_signal,
    build_suspend_intent,
    binding_vm_assignment,
    binding_vm_ref,
    claim_binding_vm_assignment_attempt,
    create_or_update_assistant_session,
    create_or_update_bootstrap_secret,
    delete_assistant_session,
    desktop_url_matches_vm_ref,
    get_latest_unity_image,
    merge_conditions,
    patch_assistant_session_status,
    persist_binding_vm_assignment_result,
    preview_image_override,
    record_released_binding,
    record_assistant_session_signal,
    released_binding,
    session_signal,
    session_released_bindings,
    session_suspend_intent,
    suspend_intent_value,
    vm_refs_match,
)
from communication.infra.observability import bind_causal_context, build_causal_context
from kubernetes.client.rest import ApiException


def _fake_secret(
    payload: dict,
    resource_version: str = "1",
    *,
    name: str = "assistant-session-bootstrap-1207",
    annotations: dict | None = None,
):
    class FakeSecret:
        def __init__(self):
            self.metadata = type(
                "Metadata",
                (),
                {
                    "resource_version": resource_version,
                    "name": name,
                    "annotations": annotations or {},
                },
            )()
            self.data = {
                "startup.json": base64.b64encode(
                    json.dumps(payload).encode("utf-8"),
                ).decode("utf-8"),
            }
            self.string_data = None

    return FakeSecret()


def test_assistant_session_names_are_sanitized():
    assert assistant_session_name("ABC_123") == "assistant-session-abc-123"
    assert (
        assistant_session_secret_name("ABC_123", "Act_456")
        == "assistant-session-bootstrap-abc-123-act-456"
    )


def test_build_assistant_session_spec_sets_desktop_required():
    spec = build_assistant_session_spec(
        assistant_id="42",
        user_id="7",
        medium="unify_message",
        desktop_mode="ubuntu",
        startup_secret_ref="session-bootstrap-42",
        activation_id="act-1",
    )
    assert spec["assistantId"] == "42"
    assert spec["userId"] == "7"
    assert spec["desiredState"] == "Running"
    assert spec["desktop"] == {"required": True, "mode": "ubuntu"}
    assert spec["startupSecretRef"] == "session-bootstrap-42"
    assert spec["activationId"] == "act-1"


def test_preview_image_override_returns_none_without_branch_tag(monkeypatch):
    monkeypatch.setattr(assistant_sessions_module.SETTINGS, "branch_tag", "")

    def _fail():
        raise AssertionError("should not call GCS without a branch tag")

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_latest_unity_image",
        _fail,
    )

    assert preview_image_override() is None


def test_preview_image_override_resolves_image_when_branch_tag_set(monkeypatch):
    monkeypatch.setattr(
        assistant_sessions_module.SETTINGS,
        "branch_tag",
        "myslug",
    )
    monkeypatch.setattr(
        assistant_sessions_module,
        "get_latest_unity_image",
        lambda: "registry/unity-staging:preview-myslug-deadbeef",
    )

    assert preview_image_override() == (
        "registry/unity-staging:preview-myslug-deadbeef"
    )


def test_terminating_session_is_treated_as_stopped():
    session = {
        "metadata": {"deletionTimestamp": "2026-04-05T15:39:56Z"},
        "spec": {"desiredState": "Running"},
    }

    assert assistant_session_is_terminating(session) is True
    assert assistant_session_desired_state(session) == "Stopped"


def test_merge_conditions_replaces_by_type():
    original = [build_condition("ContainerAssigned", False, "Pending")]
    merged = merge_conditions(
        original,
        build_condition("ContainerAssigned", True, "Bound"),
        build_condition("Active", False, "Waiting"),
    )
    as_map = {condition["type"]: condition for condition in merged}
    assert as_map["ContainerAssigned"]["status"] == "True"
    assert as_map["ContainerAssigned"]["reason"] == "Bound"
    assert as_map["Active"]["status"] == "False"


def test_vm_refs_match_requires_same_identity():
    assert vm_refs_match(
        {
            "name": "unity-pool-ubuntu-10-staging",
            "hostname": "unity-pool-ubuntu-10-staging.vm.unify.ai",
        },
        {
            "name": "unity-pool-ubuntu-10-staging",
            "hostname": "https://unity-pool-ubuntu-10-staging.vm.unify.ai/",
        },
    )
    assert not vm_refs_match(
        {
            "name": "unity-pool-ubuntu-10-staging",
            "hostname": "unity-pool-ubuntu-10-staging.vm.unify.ai",
        },
        {
            "name": "unity-pool-ubuntu-14-staging",
            "hostname": "unity-pool-ubuntu-14-staging.vm.unify.ai",
        },
    )


def test_desktop_url_matches_vm_ref_normalizes_scheme():
    vm_ref = {
        "name": "unity-pool-ubuntu-10-staging",
        "hostname": "unity-pool-ubuntu-10-staging.vm.unify.ai",
    }
    assert desktop_url_matches_vm_ref(
        "https://unity-pool-ubuntu-10-staging.vm.unify.ai/",
        vm_ref,
    )
    assert not desktop_url_matches_vm_ref(
        "https://unity-pool-ubuntu-14-staging.vm.unify.ai",
        vm_ref,
    )


def test_assistant_session_observability_fields_summarize_runtime_state():
    session = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {
            "assistantId": "1207",
            "activationId": "act-1",
            "desiredState": "Running",
        },
        "status": {
            "phase": "PendingVM",
            "observedActivationId": "act-1",
            "binding": build_binding(
                binding_id="binding-1",
                job_ref={"name": "unity-job-1"},
                pod_ref={"name": "unity-pod-1"},
                vm_ref={
                    "name": "unity-pool-ubuntu-10-staging",
                    "hostname": "unity-pool-ubuntu-10-staging.vm.unify.ai",
                },
                desktop_url="https://unity-pool-ubuntu-10-staging.vm.unify.ai",
            ),
            "lastError": "waiting",
            "suspendIntent": build_suspend_intent(
                binding_id="binding-1",
                intent=SUSPEND_INTENT_REPLACE,
                source="controller.bootstrap_timeout",
            ),
            "conditions": [
                build_condition("ContainerAssigned", True, "Bound"),
                build_condition("DesktopReady", False, "WaitingForDesktop"),
            ],
        },
    }

    summary = assistant_session_observability_fields(session)

    assert summary["assistant_id"] == "1207"
    assert summary["session_name"] == "assistant-session-1207"
    assert summary["activation_id"] == "act-1"
    assert summary["job_name"] == "unity-job-1"
    assert summary["vm_name"] == "unity-pool-ubuntu-10-staging"
    assert summary["vm_hostname"] == "unity-pool-ubuntu-10-staging.vm.unify.ai"
    assert summary["suspend_intent"] == SUSPEND_INTENT_REPLACE
    assert summary["suspend_intent_binding_id"] == "binding-1"
    assert summary["suspend_intent_source"] == "controller.bootstrap_timeout"
    assert summary["condition_states"]["ContainerAssigned"].startswith("True:")


def test_patch_assistant_session_status_allows_explicit_none(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: {
            "status": {
                "binding": build_binding(
                    binding_id="binding-1",
                    vm_ref={"name": "unity-pool-ubuntu-10-staging"},
                    desktop_url="https://unity-pool-ubuntu-10-staging.vm.unify.ai",
                ),
            },
        },
    )

    class FakeCustomApi:
        def replace_namespaced_custom_object_status(self, **kwargs):
            captured.update(kwargs)
            return kwargs["body"]

    patch_assistant_session_status(
        FakeCustomApi(),
        "staging",
        "1207",
        binding=None,
    )

    assert captured["body"]["status"]["binding"] is None


def test_patch_assistant_session_status_persists_suspend_intent(monkeypatch):
    session = {
        "metadata": {"resourceVersion": "1"},
        "status": {
            "binding": build_binding(binding_id="binding-1"),
        },
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(session),
    )

    class FakeCustomApi:
        def replace_namespaced_custom_object_status(self, **kwargs):
            next_session = deepcopy(kwargs["body"])
            next_session.setdefault("metadata", {})
            next_session["metadata"]["resourceVersion"] = "2"
            session.clear()
            session.update(next_session)
            return deepcopy(session)

    updated = patch_assistant_session_status(
        FakeCustomApi(),
        "staging",
        "1207",
        suspend_intent=build_suspend_intent(
            binding_id="binding-1",
            intent=SUSPEND_INTENT_STOP,
            source="views.session_stop",
        ),
    )

    assert session_suspend_intent(updated)["bindingId"] == "binding-1"
    assert suspend_intent_value(session_suspend_intent(updated)) == SUSPEND_INTENT_STOP


def test_patch_assistant_session_status_preserves_retry_counters(monkeypatch):
    session = {
        "metadata": {"resourceVersion": "1"},
        "status": {},
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(session),
    )

    class FakeCustomApi:
        def replace_namespaced_custom_object_status(self, **kwargs):
            next_session = deepcopy(kwargs["body"])
            next_session.setdefault("metadata", {})
            next_session["metadata"]["resourceVersion"] = str(
                int(session["metadata"]["resourceVersion"]) + 1,
            )
            session.clear()
            session.update(next_session)
            return deepcopy(session)

    custom_api = FakeCustomApi()
    patch_assistant_session_status(
        custom_api,
        "staging",
        "1207",
        bootstrap_retries=2,
        vm_retries=3,
        desktop_probe_failures=1,
    )

    patch_assistant_session_status(
        custom_api,
        "staging",
        "1207",
        phase="PendingVM",
        observed_activation_id="act-2",
    )

    assert session["status"]["bootstrapRetries"] == 2
    assert session["status"]["vmRetries"] == 3
    assert session["status"]["desktopProbeFailures"] == 1
    assert session["status"]["phase"] == "PendingVM"
    assert session["status"]["observedActivationId"] == "act-2"


def test_patch_assistant_session_status_replaces_signals_without_touching_binding(
    monkeypatch,
):
    session = {
        "metadata": {"resourceVersion": "1"},
        "status": {
            "phase": "PendingGuest",
            "binding": build_binding(
                binding_id="binding-1",
                vm_ref={"name": "unity-pool-ubuntu-10-staging"},
            ),
        },
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(session),
    )

    class FakeCustomApi:
        def replace_namespaced_custom_object_status(self, **kwargs):
            next_session = deepcopy(kwargs["body"])
            next_session.setdefault("metadata", {})
            next_session["metadata"]["resourceVersion"] = str(
                int(session["metadata"]["resourceVersion"]) + 1,
            )
            session.clear()
            session.update(next_session)
            return deepcopy(session)

    patch_assistant_session_status(
        FakeCustomApi(),
        "staging",
        "1207",
        signals={
            "desktopReady": build_binding_signal(
                binding_id="binding-1",
                state="ready",
                hostname="unity-pool-ubuntu-10-staging.vm.unify.ai",
            ),
        },
    )

    assert session["status"]["phase"] == "PendingGuest"
    assert session["status"]["binding"]["id"] == "binding-1"
    assert session["status"]["signals"]["desktopReady"]["state"] == "ready"


def test_record_assistant_session_signal_merges_into_status(monkeypatch):
    session = {
        "metadata": {"resourceVersion": "1"},
        "status": {
            "phase": "PendingGuest",
            "binding": build_binding(binding_id="binding-1"),
            "signals": {
                "vmGuestHealth": build_binding_signal(
                    binding_id="binding-1",
                    state="ready",
                    vmRef={"name": "unity-pool-ubuntu-10-staging"},
                ),
            },
        },
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(session),
    )

    class FakeCustomApi:
        def replace_namespaced_custom_object_status(self, **kwargs):
            next_session = deepcopy(kwargs["body"])
            next_session.setdefault("metadata", {})
            next_session["metadata"]["resourceVersion"] = str(
                int(session["metadata"]["resourceVersion"]) + 1,
            )
            session.clear()
            session.update(next_session)
            return deepcopy(session)

    updated = record_assistant_session_signal(
        FakeCustomApi(),
        "staging",
        "1207",
        signal_name="desktopReady",
        payload=build_binding_signal(
            binding_id="binding-1",
            state="ready",
            hostname="unity-pool-ubuntu-10-staging.vm.unify.ai",
        ),
        source="test",
    )

    assert session_signal(updated, "vmGuestHealth")["state"] == "ready"
    assert session_signal(updated, "desktopReady")["hostname"].startswith("unity-pool")


def test_record_released_binding_upserts_release_ledger(monkeypatch):
    session = {
        "metadata": {"resourceVersion": "1"},
        "status": {
            "phase": "PendingJob",
            "binding": build_binding(binding_id="binding-2"),
            "releasedBindings": [
                {
                    "bindingId": "binding-1",
                    "releaseRequestedAt": "2026-04-08T00:00:00+00:00",
                    "releaseCompletedAt": "2026-04-08T00:01:00+00:00",
                },
            ],
        },
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(session),
    )

    class FakeCustomApi:
        def replace_namespaced_custom_object_status(self, **kwargs):
            next_session = deepcopy(kwargs["body"])
            next_session.setdefault("metadata", {})
            next_session["metadata"]["resourceVersion"] = str(
                int(session["metadata"]["resourceVersion"]) + 1,
            )
            session.clear()
            session.update(next_session)
            return deepcopy(session)

    updated = record_released_binding(
        FakeCustomApi(),
        "staging",
        "1207",
        binding_id="binding-2",
        release_requested_at="2026-04-08T00:02:00+00:00",
        release_completed_at="2026-04-08T00:03:00+00:00",
        source="test",
    )

    assert len(session_released_bindings(updated)) == 2
    assert released_binding(updated, "binding-2") == {
        "bindingId": "binding-2",
        "releaseRequestedAt": "2026-04-08T00:02:00+00:00",
        "releaseCompletedAt": "2026-04-08T00:03:00+00:00",
    }


def test_claim_binding_vm_assignment_attempt_marks_binding_in_progress(monkeypatch):
    session = {
        "metadata": {"resourceVersion": "1"},
        "spec": {"desiredState": "Running"},
        "status": {
            "phase": "PendingVM",
            "binding": build_binding(binding_id="binding-1"),
            "signals": {},
        },
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(session),
    )

    class FakeCustomApi:
        def replace_namespaced_custom_object_status(self, **kwargs):
            next_session = deepcopy(kwargs["body"])
            next_session.setdefault("metadata", {})
            next_session["metadata"]["resourceVersion"] = str(
                int(session["metadata"]["resourceVersion"]) + 1,
            )
            session.clear()
            session.update(next_session)
            return deepcopy(session)

    attempt_id = claim_binding_vm_assignment_attempt(
        FakeCustomApi(),
        "staging",
        "1207",
        target_binding_id="binding-1",
        stale_after_seconds=60,
        source="test",
    )

    assert attempt_id
    assignment = binding_vm_assignment(session["status"]["binding"])
    assert assignment["state"] == "in_progress"
    assert assignment["attemptId"] == attempt_id


def test_persist_binding_vm_assignment_result_records_success(monkeypatch):
    session = {
        "metadata": {"resourceVersion": "1"},
        "spec": {"desiredState": "Running"},
        "status": {
            "phase": "PendingVM",
            "binding": build_binding(
                binding_id="binding-1",
                vm_assignment=build_binding_vm_assignment(
                    attempt_id="attempt-1",
                    state="in_progress",
                ),
            ),
            "signals": {},
        },
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(session),
    )

    class FakeCustomApi:
        def replace_namespaced_custom_object_status(self, **kwargs):
            next_session = deepcopy(kwargs["body"])
            next_session.setdefault("metadata", {})
            next_session["metadata"]["resourceVersion"] = str(
                int(session["metadata"]["resourceVersion"]) + 1,
            )
            session.clear()
            session.update(next_session)
            return deepcopy(session)

    persisted = persist_binding_vm_assignment_result(
        FakeCustomApi(),
        "staging",
        "1207",
        target_binding_id="binding-1",
        attempt_id="attempt-1",
        state="assigned",
        vm_ref={
            "name": "unity-pool-ubuntu-10-staging",
            "hostname": "unity-pool-ubuntu-10-staging.vm.unify.ai",
            "vmType": "ubuntu",
        },
        source="test",
    )

    assert persisted is True
    assert (
        binding_vm_ref(session["status"]["binding"])["name"]
        == "unity-pool-ubuntu-10-staging"
    )
    assert binding_vm_assignment(session["status"]["binding"]) == {}


def test_persist_binding_vm_assignment_result_ignores_stale_attempt(monkeypatch):
    session = {
        "metadata": {"resourceVersion": "1"},
        "spec": {"desiredState": "Running"},
        "status": {
            "phase": "PendingVM",
            "binding": build_binding(
                binding_id="binding-1",
                vm_assignment=build_binding_vm_assignment(
                    attempt_id="attempt-2",
                    state="in_progress",
                ),
            ),
            "signals": {},
        },
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(session),
    )

    class FakeCustomApi:
        def replace_namespaced_custom_object_status(self, **kwargs):
            next_session = deepcopy(kwargs["body"])
            next_session.setdefault("metadata", {})
            next_session["metadata"]["resourceVersion"] = str(
                int(session["metadata"]["resourceVersion"]) + 1,
            )
            session.clear()
            session.update(next_session)
            return deepcopy(session)

    persisted = persist_binding_vm_assignment_result(
        FakeCustomApi(),
        "staging",
        "1207",
        target_binding_id="binding-1",
        attempt_id="attempt-1",
        state="capacity",
        message="Waiting for VM capacity",
        source="test",
    )

    assert persisted is False
    assignment = binding_vm_assignment(session["status"]["binding"])
    assert assignment["attemptId"] == "attempt-2"
    assert assignment["state"] == "in_progress"


def test_emit_observability_event_includes_bound_causal_context(caplog):
    with caplog.at_level(logging.INFO, logger=assistant_sessions_module.logger.name):
        with bind_causal_context(
            build_causal_context(
                caller="tests.assistant_sessions",
                reason="unit_test",
            ),
        ):
            assistant_sessions_module.emit_observability_event(
                "tests.observability",
                assistant_id="1207",
            )

    event_payload = json.loads(caplog.records[-1].message.removeprefix("OBS_EVENT "))
    assert event_payload["event"] == "tests.observability"
    assert event_payload["assistant_id"] == "1207"
    assert event_payload["caller"] == "tests.assistant_sessions"
    assert event_payload["root_caller"] == "tests.assistant_sessions"
    assert event_payload["reason"] == "unit_test"
    assert event_payload["operation_id"]


def test_record_assistant_session_signal_persists_source_and_causal_context(
    monkeypatch,
):
    session = {
        "metadata": {"resourceVersion": "1"},
        "status": {
            "phase": "PendingGuest",
            "binding": build_binding(binding_id="binding-1"),
            "signals": {},
        },
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(session),
    )

    class FakeCustomApi:
        def replace_namespaced_custom_object_status(self, **kwargs):
            next_session = deepcopy(kwargs["body"])
            next_session.setdefault("metadata", {})
            next_session["metadata"]["resourceVersion"] = str(
                int(session["metadata"]["resourceVersion"]) + 1,
            )
            session.clear()
            session.update(next_session)
            return deepcopy(session)

    with bind_causal_context(
        build_causal_context(
            caller="tests.assistant_sessions",
            reason="unit_test",
        ),
    ):
        updated = record_assistant_session_signal(
            FakeCustomApi(),
            "staging",
            "1207",
            signal_name="desktopReady",
            payload=build_binding_signal(
                binding_id="binding-1",
                state="ready",
                hostname="unity-pool-ubuntu-10-staging.vm.unify.ai",
            ),
            source="test",
        )

    signal = session_signal(updated, "desktopReady")
    assert signal["source"] == "test"
    assert signal["causal"]["caller"] == "tests.assistant_sessions"
    assert signal["causal"]["rootCaller"] == "tests.assistant_sessions"
    assert signal["causal"]["reason"] == "unit_test"
    assert signal["causal"]["operationId"]


def test_patch_assistant_session_status_replaces_binding_atomically(monkeypatch):
    session = {
        "metadata": {"name": "assistant-session-1207", "resourceVersion": "7"},
        "status": {
            "phase": "Active",
            "binding": build_binding(
                binding_id="binding-1",
                job_ref={"name": "unity-job-1", "namespace": "staging"},
                pod_ref={"name": "unity-pod-1", "namespace": "staging"},
                vm_ref={
                    "name": "unity-pool-ubuntu-10-staging",
                    "hostname": "unity-pool-ubuntu-10-staging.vm.unify.ai",
                },
                desktop_url="https://unity-pool-ubuntu-10-staging.vm.unify.ai",
            ),
        },
    }
    replace_calls = 0
    patch_calls = 0

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(session),
    )

    class FakeCustomApi:
        def patch_namespaced_custom_object_status(self, **kwargs):
            nonlocal patch_calls
            patch_calls += 1
            binding = session.setdefault("status", {}).setdefault("binding", {})
            binding.update(deepcopy(kwargs["body"]["status"].get("binding") or {}))
            return deepcopy(session)

        def replace_namespaced_custom_object_status(self, **kwargs):
            nonlocal replace_calls
            replace_calls += 1
            next_session = deepcopy(kwargs["body"])
            next_session.setdefault("metadata", {})
            next_session["metadata"]["resourceVersion"] = str(
                int(session["metadata"]["resourceVersion"]) + 1,
            )
            session.clear()
            session.update(next_session)
            return deepcopy(session)

    patch_assistant_session_status(
        FakeCustomApi(),
        "staging",
        "1207",
        phase="PendingJob",
        binding=build_binding(
            binding_id="binding-2",
            created_at="2026-04-06T00:00:00+00:00",
        ),
        last_error="Recorded binding Job disappeared before runtime became ready",
    )

    assert replace_calls == 1
    assert patch_calls == 0
    assert session["status"]["phase"] == "PendingJob"
    assert session["status"]["binding"] == {
        "id": "binding-2",
        "createdAt": "2026-04-06T00:00:00+00:00",
    }
    assert "jobRef" not in session["status"]["binding"]
    assert "podRef" not in session["status"]["binding"]
    assert "vmRef" not in session["status"]["binding"]
    assert "desktopUrl" not in session["status"]["binding"]


def test_crd_status_schema_covers_all_persisted_status_fields():
    crd_path = (
        Path(__file__).resolve().parents[2]
        / "k8s"
        / "assistant-session-controller"
        / "crd.yaml"
    )
    crd = yaml.safe_load(crd_path.read_text())
    status_properties = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"][
        "properties"
    ]["status"]["properties"]

    expected_fields = {
        "phase",
        "observedActivationId",
        "binding",
        "lastError",
        "conditions",
        "bootstrapRetries",
        "vmRetries",
        "desktopProbeFailures",
        "suspendIntent",
        "signals",
        "releasedBindings",
    }
    assert expected_fields.issubset(status_properties.keys())
    suspend_intent_properties = status_properties["suspendIntent"]["properties"]
    assert {
        "bindingId",
        "intent",
        "source",
        "sourceReason",
        "requestedAt",
        "jobName",
    }.issubset(suspend_intent_properties.keys())
    binding_properties = status_properties["binding"]["properties"]
    assert {
        "id",
        "jobRef",
        "podRef",
        "vmRef",
        "desktopUrl",
        "containerBootstrapStartedAt",
        "guestHandshakeStartedAt",
        "releaseGeneration",
    }.issubset(binding_properties.keys())
    released_binding_properties = status_properties["releasedBindings"]["items"][
        "properties"
    ]
    assert {"bindingId", "releaseRequestedAt", "releaseCompletedAt"}.issubset(
        released_binding_properties.keys(),
    )


def test_delete_assistant_session_treats_missing_session_as_absent():
    class FakeCustomApi:
        def delete_namespaced_custom_object(self, **_kwargs):
            raise ApiException(status=404)

    deleted = delete_assistant_session(
        FakeCustomApi(),
        "staging",
        "1207",
    )

    assert deleted is False


def test_create_or_update_bootstrap_secret_reconciles_create_conflict_to_latest_payload():
    requested_payload = {
        "api_key": "latest-secret",
        "assistant_about": "fresh payload",
    }

    class FakeCoreApi:
        def __init__(self):
            self.resource_version = "1"
            self.stored_payload = {"api_key": "stale-secret"}
            self.read_count = 0
            self.replace_count = 0

        def read_namespaced_secret(self, **_kwargs):
            self.read_count += 1
            if self.read_count == 1:
                raise ApiException(status=404)
            return _fake_secret(
                self.stored_payload,
                resource_version=self.resource_version,
                name=assistant_session_secret_name("1207", "act-1"),
            )

        def create_namespaced_secret(self, **_kwargs):
            raise ApiException(status=409)

        def replace_namespaced_secret(self, **kwargs):
            self.replace_count += 1
            self.stored_payload = json.loads(kwargs["body"].string_data["startup.json"])
            self.resource_version = str(int(self.resource_version) + 1)

    core_api = FakeCoreApi()
    secret_name = create_or_update_bootstrap_secret(
        core_api,
        "staging",
        "1207",
        "act-1",
        requested_payload,
    )

    assert secret_name == "assistant-session-bootstrap-1207-act-1"
    assert core_api.replace_count == 1
    assert core_api.stored_payload == requested_payload


def test_create_or_update_bootstrap_secret_retries_replace_conflict():
    call_log = []
    requested_payload = {"api_key": "latest-secret"}
    stored_payload = {"api_key": "stale-secret"}

    class FakeCoreApi:
        def __init__(self):
            self.resource_version = "1"

        def read_namespaced_secret(self, **_kwargs):
            call_log.append("read")
            return _fake_secret(
                stored_payload,
                resource_version=self.resource_version,
                name=assistant_session_secret_name("1207", "act-1"),
            )

        def replace_namespaced_secret(self, **kwargs):
            call_log.append("replace")
            if call_log.count("replace") < 3:
                raise ApiException(status=409)
            stored_payload.update(
                json.loads(kwargs["body"].string_data["startup.json"]),
            )
            self.resource_version = str(int(self.resource_version) + 1)

    secret_name = create_or_update_bootstrap_secret(
        FakeCoreApi(),
        "staging",
        "1207",
        "act-1",
        requested_payload,
    )

    assert secret_name == "assistant-session-bootstrap-1207-act-1"
    assert call_log.count("replace") == 3
    assert call_log.count("read") == 3
    assert stored_payload == requested_payload


def test_create_or_update_bootstrap_secret_replaces_when_owner_annotations_stale():
    requested_payload = {"api_key": "latest-secret"}
    replace_calls = []

    class FakeCoreApi:
        def read_namespaced_secret(self, **_kwargs):
            return _fake_secret(
                requested_payload,
                name=assistant_session_secret_name("1207", "act-new"),
                annotations={
                    SESSION_REF_ANNOTATION: assistant_session_name("1207"),
                    ACTIVATION_ID_ANNOTATION: "act-old",
                },
            )

        def replace_namespaced_secret(self, **kwargs):
            replace_calls.append(kwargs["body"])

    secret_name = create_or_update_bootstrap_secret(
        FakeCoreApi(),
        "staging",
        "1207",
        "act-new",
        requested_payload,
    )

    assert secret_name == "assistant-session-bootstrap-1207-act-new"
    assert len(replace_calls) == 1
    assert replace_calls[0].metadata.annotations == {
        SESSION_REF_ANNOTATION: assistant_session_name("1207"),
        ACTIVATION_ID_ANNOTATION: "act-new",
    }


def test_create_or_update_assistant_session_rejects_terminating_existing_session(
    monkeypatch,
):
    desired_spec = build_assistant_session_spec(
        assistant_id="1207",
        user_id="7",
        medium="unify_message",
        desktop_mode="ubuntu",
        startup_secret_ref="assistant-session-bootstrap-1207",
        activation_id="act-1",
    )
    terminating_session = {
        "metadata": {
            "name": "assistant-session-1207",
            "resourceVersion": "7",
            "deletionTimestamp": "2026-04-06T12:00:00Z",
        },
        "spec": {"activationId": "act-1"},
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(terminating_session),
    )

    class FakeCustomApi:
        def create_namespaced_custom_object(self, **_kwargs):
            raise AssertionError("terminating sessions must not be created over")

        def patch_namespaced_custom_object(self, **_kwargs):
            raise AssertionError("terminating sessions must not be patched")

    with pytest.raises(AssistantSessionTerminatingError):
        create_or_update_assistant_session(
            FakeCustomApi(),
            "staging",
            "1207",
            desired_spec,
        )


def test_create_or_update_assistant_session_skips_patch_when_spec_already_current(
    monkeypatch,
):
    desired_spec = build_assistant_session_spec(
        assistant_id="1207",
        user_id="7",
        medium="unify_message",
        desktop_mode="ubuntu",
        startup_secret_ref="assistant-session-bootstrap-1207",
        activation_id="act-1",
    )
    current_session = {
        "metadata": {"name": "assistant-session-1207", "resourceVersion": "7"},
        "spec": {
            **desired_spec,
            "requestedAt": "2026-04-02T15:00:00+00:00",
        },
    }

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: current_session,
    )

    class FakeCustomApi:
        def patch_namespaced_custom_object(self, **_kwargs):
            raise AssertionError("spec already converged; patch should not run")

    session = create_or_update_assistant_session(
        FakeCustomApi(),
        "staging",
        "1207",
        desired_spec,
    )

    assert session is current_session


def test_create_or_update_assistant_session_returns_existing_on_create_conflict_when_semantically_current(
    monkeypatch,
):
    desired_spec = build_assistant_session_spec(
        assistant_id="1207",
        user_id="7",
        medium="unify_message",
        desktop_mode="ubuntu",
        startup_secret_ref="assistant-session-bootstrap-1207",
        activation_id="act-1",
    )
    existing_session = {
        "metadata": {"name": "assistant-session-1207", "resourceVersion": "1"},
        "spec": {
            **desired_spec,
            "requestedAt": "2026-04-02T15:00:00+00:00",
        },
    }
    calls = {"count": 0}

    def _get_session(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return existing_session

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        _get_session,
    )

    class FakeCustomApi:
        def create_namespaced_custom_object(self, **_kwargs):
            raise ApiException(status=409)

        def patch_namespaced_custom_object(self, **_kwargs):
            raise AssertionError(
                "converged spec should not be patched after create race",
            )

    session = create_or_update_assistant_session(
        FakeCustomApi(),
        "staging",
        "1207",
        desired_spec,
    )

    assert session is existing_session


def test_create_or_update_assistant_session_retries_when_create_conflict_reread_is_missing(
    monkeypatch,
):
    desired_spec = build_assistant_session_spec(
        assistant_id="1207",
        user_id="7",
        medium="unify_message",
        desktop_mode="ubuntu",
        startup_secret_ref="assistant-session-bootstrap-1207",
        activation_id="act-1",
    )
    existing_session = {
        "metadata": {"name": "assistant-session-1207", "resourceVersion": "2"},
        "spec": {
            **desired_spec,
            "requestedAt": "2026-04-02T15:00:00+00:00",
        },
    }
    calls = {"count": 0}

    def _get_session(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            return None
        return existing_session

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        _get_session,
    )

    class FakeCustomApi:
        def create_namespaced_custom_object(self, **_kwargs):
            raise ApiException(status=409)

        def patch_namespaced_custom_object(self, **_kwargs):
            raise AssertionError("converged spec should not be patched")

    session = create_or_update_assistant_session(
        FakeCustomApi(),
        "staging",
        "1207",
        desired_spec,
    )

    assert session is existing_session


def test_create_or_update_assistant_session_raises_on_create_conflict_when_spec_differs(
    monkeypatch,
):
    desired_spec = build_assistant_session_spec(
        assistant_id="1207",
        user_id="7",
        medium="unify_message",
        desktop_mode="ubuntu",
        startup_secret_ref="assistant-session-bootstrap-1207",
        activation_id="act-1",
    )
    stored_session = {
        "metadata": {"name": "assistant-session-1207", "resourceVersion": "1"},
        "spec": {
            **desired_spec,
            "medium": "email",
            "requestedAt": "2026-04-02T15:00:00+00:00",
        },
    }
    reads = {"count": 0}

    def _get_session(*_args, **_kwargs):
        reads["count"] += 1
        return deepcopy(stored_session) if reads["count"] > 1 else None

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        _get_session,
    )
    patch_calls = {"count": 0}

    class FakeCustomApi:
        def create_namespaced_custom_object(self, **_kwargs):
            raise ApiException(status=409)

        def patch_namespaced_custom_object(self, **kwargs):
            patch_calls["count"] += 1
            stored_session["spec"] = deepcopy(kwargs["body"]["spec"])
            stored_session["metadata"]["resourceVersion"] = "2"
            return deepcopy(stored_session)

    with pytest.raises(ApiException) as exc_info:
        create_or_update_assistant_session(
            FakeCustomApi(),
            "staging",
            "1207",
            desired_spec,
        )

    assert exc_info.value.status == 409
    assert patch_calls["count"] == 0
    assert stored_session["spec"]["medium"] == "email"
    assert stored_session["spec"]["activationId"] == "act-1"


def test_create_or_update_assistant_session_applies_requested_activation(
    monkeypatch,
):
    desired_spec = build_assistant_session_spec(
        assistant_id="1207",
        user_id="7",
        medium="unify_message",
        desktop_mode="ubuntu",
        startup_secret_ref="assistant-session-bootstrap-1207",
        activation_id="act-loser",
    )
    stored_session = {
        "metadata": {"name": "assistant-session-1207", "resourceVersion": "1"},
        "spec": {
            **desired_spec,
            "medium": "email",
            "requestedAt": "2026-04-02T15:00:00+00:00",
        },
    }
    patched_specs = []

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(stored_session),
    )

    class FakeCustomApi:
        def patch_namespaced_custom_object(self, **kwargs):
            patched_specs.append(deepcopy(kwargs["body"]["spec"]))
            stored_session["spec"] = deepcopy(kwargs["body"]["spec"])
            stored_session["metadata"]["resourceVersion"] = "2"
            return deepcopy(stored_session)

    session = create_or_update_assistant_session(
        FakeCustomApi(),
        "staging",
        "1207",
        desired_spec,
    )

    assert patched_specs == [desired_spec]
    assert session["spec"]["activationId"] == "act-loser"
    assert session["spec"]["medium"] == "unify_message"


def test_create_or_update_assistant_session_converges_after_patch_conflict_when_reread_matches(
    monkeypatch,
):
    desired_spec = build_assistant_session_spec(
        assistant_id="1207",
        user_id="7",
        medium="unify_message",
        desktop_mode="ubuntu",
        startup_secret_ref="assistant-session-bootstrap-1207",
        activation_id="act-1",
    )
    stale_session = {
        "metadata": {"name": "assistant-session-1207", "resourceVersion": "1"},
        "spec": {
            **desired_spec,
            "medium": "email",
            "requestedAt": "2026-04-02T15:00:00+00:00",
        },
    }
    converged_session = {
        "metadata": {"name": "assistant-session-1207", "resourceVersion": "2"},
        "spec": {
            **desired_spec,
            "requestedAt": "2026-04-02T15:01:00+00:00",
        },
    }
    reads = iter([stale_session, converged_session])
    patch_calls = {"count": 0}

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: deepcopy(next(reads)),
    )

    class FakeCustomApi:
        def patch_namespaced_custom_object(self, **_kwargs):
            patch_calls["count"] += 1
            raise ApiException(status=409)

    session = create_or_update_assistant_session(
        FakeCustomApi(),
        "staging",
        "1207",
        desired_spec,
    )

    assert patch_calls["count"] == 1
    assert session["spec"]["medium"] == "unify_message"


def test_get_latest_unity_image_uses_inline_service_account_when_present(
    monkeypatch,
):
    from google.cloud import storage
    from google.oauth2.service_account import Credentials

    seen = {}
    sentinel_creds = object()

    def _from_service_account_info(info):
        seen["info"] = info
        return sentinel_creds

    class FakeBlob:
        def download_as_text(self):
            return "abc123\n"

    class FakeBucket:
        def blob(self, name):
            seen["blob"] = name
            return FakeBlob()

    class FakeStorageClient:
        def __init__(self, credentials=None):
            seen["credentials"] = credentials

        def bucket(self, name):
            seen["bucket"] = name
            return FakeBucket()

    monkeypatch.setenv("GCP_SA_KEY", json.dumps({"client_email": "svc@example.com"}))
    monkeypatch.setattr(
        Credentials,
        "from_service_account_info",
        _from_service_account_info,
    )
    monkeypatch.setattr(storage, "Client", FakeStorageClient)

    image = get_latest_unity_image()

    assert seen["info"] == {"client_email": "svc@example.com"}
    assert seen["credentials"] is sentinel_creds
    assert seen["bucket"] == "unity-image-hash"
    assert seen["blob"] == assistant_sessions_module.SETTINGS.image_hash_blob
    assert (
        image == f"{assistant_sessions_module.SETTINGS.image_registry}/"
        f"{assistant_sessions_module.SETTINGS.unity_image_name}:abc123"
    )


def test_get_latest_unity_image_falls_back_to_adc_when_inline_key_missing(
    monkeypatch,
):
    from google.cloud import storage
    from google.oauth2.service_account import Credentials

    seen = {}

    class FakeBlob:
        def download_as_text(self):
            return "def456\n"

    class FakeBucket:
        def blob(self, _name):
            return FakeBlob()

    class FakeStorageClient:
        def __init__(self, credentials=None):
            seen["credentials"] = credentials

        def bucket(self, _name):
            return FakeBucket()

    monkeypatch.delenv("GCP_SA_KEY", raising=False)
    monkeypatch.setattr(
        Credentials,
        "from_service_account_info",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("inline credentials should not be used without GCP_SA_KEY"),
        ),
    )
    monkeypatch.setattr(storage, "Client", FakeStorageClient)

    image = get_latest_unity_image()

    assert seen["credentials"] is None
    assert (
        image == f"{assistant_sessions_module.SETTINGS.image_registry}/"
        f"{assistant_sessions_module.SETTINGS.unity_image_name}:def456"
    )
