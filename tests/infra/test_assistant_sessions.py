from communication.infra.assistant_sessions import (
    assistant_session_name,
    assistant_session_secret_name,
    build_assistant_session_spec,
    build_condition,
    merge_conditions,
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
