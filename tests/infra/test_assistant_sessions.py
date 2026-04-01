from copy import deepcopy
from pathlib import Path

import yaml

from communication.infra import assistant_sessions as assistant_sessions_module
from communication.infra.assistant_sessions import (
    assistant_session_name,
    assistant_session_observability_fields,
    assistant_session_secret_name,
    build_assistant_session_spec,
    build_condition,
    create_or_update_assistant_session,
    create_or_update_bootstrap_secret,
    delete_assistant_session,
    desktop_url_matches_vm_ref,
    merge_conditions,
    patch_assistant_session_status,
    vm_refs_match,
)
from kubernetes.client.rest import ApiException


def test_assistant_session_names_are_sanitized():
    assert assistant_session_name("ABC_123") == "assistant-session-abc-123"
    assert (
        assistant_session_secret_name("ABC_123")
        == "assistant-session-bootstrap-abc-123"
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
    assert spec["desktopRequired"] is True
    assert spec["desktopMode"] == "ubuntu"
    assert spec["startupSecretRef"] == "session-bootstrap-42"
    assert spec["activationId"] == "act-1"


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
            "name": "unity-pool-ubuntu-10-preview",
            "hostname": "unity-pool-ubuntu-10-preview.vm.unify.ai",
        },
        {
            "name": "unity-pool-ubuntu-10-preview",
            "hostname": "https://unity-pool-ubuntu-10-preview.vm.unify.ai/",
        },
    )
    assert not vm_refs_match(
        {
            "name": "unity-pool-ubuntu-10-preview",
            "hostname": "unity-pool-ubuntu-10-preview.vm.unify.ai",
        },
        {
            "name": "unity-pool-ubuntu-14-preview",
            "hostname": "unity-pool-ubuntu-14-preview.vm.unify.ai",
        },
    )


def test_desktop_url_matches_vm_ref_normalizes_scheme():
    vm_ref = {
        "name": "unity-pool-ubuntu-10-preview",
        "hostname": "unity-pool-ubuntu-10-preview.vm.unify.ai",
    }
    assert desktop_url_matches_vm_ref(
        "https://unity-pool-ubuntu-10-preview.vm.unify.ai/",
        vm_ref,
    )
    assert not desktop_url_matches_vm_ref(
        "https://unity-pool-ubuntu-14-preview.vm.unify.ai",
        vm_ref,
    )


def test_assistant_session_observability_fields_summarize_runtime_state():
    session = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {"assistantId": "1207", "activationId": "act-1"},
        "status": {
            "phase": "PendingVM",
            "observedActivationId": "act-1",
            "jobRef": {"name": "unity-job-1"},
            "podRef": {"name": "unity-pod-1"},
            "vmRef": {
                "name": "unity-pool-ubuntu-10-preview",
                "hostname": "unity-pool-ubuntu-10-preview.vm.unify.ai",
            },
            "desktopUrl": "https://unity-pool-ubuntu-10-preview.vm.unify.ai",
            "lastError": "waiting",
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
    assert summary["vm_name"] == "unity-pool-ubuntu-10-preview"
    assert summary["vm_hostname"] == "unity-pool-ubuntu-10-preview.vm.unify.ai"
    assert summary["condition_states"]["ContainerAssigned"].startswith("True:")


def test_patch_assistant_session_status_allows_explicit_none(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        assistant_sessions_module,
        "get_assistant_session",
        lambda *_args, **_kwargs: {
            "status": {
                "vmRef": {"name": "unity-pool-ubuntu-10-preview"},
                "desktopUrl": "https://unity-pool-ubuntu-10-preview.vm.unify.ai",
            },
        },
    )

    class FakeCustomApi:
        def patch_namespaced_custom_object_status(self, **kwargs):
            captured.update(kwargs)
            return kwargs["body"]

    patch_assistant_session_status(
        FakeCustomApi(),
        "preview",
        "1207",
        vm_ref=None,
        desktop_url=None,
    )

    assert captured["body"]["status"]["vmRef"] is None
    assert captured["body"]["status"]["desktopUrl"] is None


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
        def patch_namespaced_custom_object_status(self, **kwargs):
            session["status"].update(kwargs["body"]["status"])
            session["metadata"]["resourceVersion"] = str(
                int(session["metadata"]["resourceVersion"]) + 1,
            )
            return deepcopy(session)

    custom_api = FakeCustomApi()
    patch_assistant_session_status(
        custom_api,
        "preview",
        "1207",
        bootstrap_retries=2,
        vm_retries=3,
        desktop_probe_failures=1,
    )

    patch_assistant_session_status(
        custom_api,
        "preview",
        "1207",
        phase="PendingVM",
        observed_activation_id="act-2",
    )

    assert session["status"]["bootstrapRetries"] == 2
    assert session["status"]["vmRetries"] == 3
    assert session["status"]["desktopProbeFailures"] == 1
    assert session["status"]["phase"] == "PendingVM"
    assert session["status"]["observedActivationId"] == "act-2"


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
        "jobRef",
        "podRef",
        "vmRef",
        "desktopUrl",
        "lastError",
        "conditions",
        "bootstrapRetries",
        "vmRetries",
        "desktopProbeFailures",
    }
    assert expected_fields.issubset(status_properties.keys())


def test_delete_assistant_session_treats_missing_session_as_absent():
    class FakeCustomApi:
        def delete_namespaced_custom_object(self, **_kwargs):
            raise ApiException(status=404)

    deleted = delete_assistant_session(
        FakeCustomApi(),
        "preview",
        "1207",
    )

    assert deleted is False


def test_create_or_update_bootstrap_secret_treats_create_conflict_as_success():
    class FakeCoreApi:
        def read_namespaced_secret(self, **_kwargs):
            raise ApiException(status=404)

        def create_namespaced_secret(self, **_kwargs):
            raise ApiException(status=409)

    secret_name = create_or_update_bootstrap_secret(
        FakeCoreApi(),
        "preview",
        "1207",
        {"api_key": "secret"},
    )

    assert secret_name == "assistant-session-bootstrap-1207"


def test_create_or_update_bootstrap_secret_retries_replace_conflict():
    call_log = []

    class FakeSecret:
        class metadata:
            resource_version = "1"

    class FakeCoreApi:
        def read_namespaced_secret(self, **_kwargs):
            call_log.append("read")
            return FakeSecret()

        def replace_namespaced_secret(self, **_kwargs):
            call_log.append("replace")
            if call_log.count("replace") < 3:
                raise ApiException(status=409)

    secret_name = create_or_update_bootstrap_secret(
        FakeCoreApi(),
        "preview",
        "1207",
        {"api_key": "secret"},
    )

    assert secret_name == "assistant-session-bootstrap-1207"
    assert call_log.count("replace") == 3
    assert call_log.count("read") == 3


def test_create_or_update_assistant_session_returns_existing_on_create_conflict(
    monkeypatch,
):
    existing_session = {
        "metadata": {"name": "assistant-session-1207"},
        "spec": {"activationId": "canonical-activation"},
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

    session = create_or_update_assistant_session(
        FakeCustomApi(),
        "preview",
        "1207",
        {"assistantId": "1207", "activationId": "racing-activation"},
    )

    assert session is existing_session
