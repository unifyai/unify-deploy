from communication.infra import assistant_sessions as assistant_sessions_module
from communication.infra.assistant_sessions import (
    assistant_session_name,
    assistant_session_secret_name,
    build_assistant_session_spec,
    build_condition,
    desktop_url_matches_vm_ref,
    merge_conditions,
    patch_assistant_session_status,
    vm_refs_match,
)


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
