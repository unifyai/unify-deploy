"""Unit tests for dashboard action offline dispatch."""


def test_dashboard_action_env_marks_assistant_as_non_coordinator():
    """Dashboard actions should run through the non-Coordinator offline lane."""

    from communication.infra.dashboard_actions import (
        DashboardActionDispatchRequest,
        _build_dashboard_action_env,
    )

    request = DashboardActionDispatchRequest(
        assistant_id="assistant-123",
        tile_token="tile-abc",
        action_name="refresh_metrics",
    )
    env = _build_dashboard_action_env(
        request=request,
        action_metadata={"function_id": 777},
        assistant_data={
            "assistant_id": "assistant-123",
            "api_key": "test-api-key",
            "is_coordinator": True,
            "self_contact_id": 42,
            "boss_contact_id": 43,
        },
        run_key="dashboard_action:assistant-123:tile-abc:refresh_metrics:abc",
        job_name="unity-dashboard-action-abc",
    )

    assert env["ASSISTANT_IS_COORDINATOR"] == "False"
    assert env["UNITY_OFFLINE_TASK_MODE"] == "function"
    assert env["UNITY_OFFLINE_TASK_FUNCTION_ID"] == "777"
    assert env["UNITY_OFFLINE_TASK_WAKE"] == "explicit"
    assert env["UNITY_OFFLINE_TASK_REVISION"] == ""
    assert env["UNITY_OFFLINE_TASK_CALL_KWARGS"] == "{}"
    assert "UNITY_OFFLINE_TASK_EXECUTION_REVISION" not in env


def test_dashboard_action_transport_defaults_to_k8s(monkeypatch):
    from communication.infra.dashboard_actions import _dashboard_action_transport

    monkeypatch.delenv("UNITY_DASHBOARD_ACTION_TRANSPORT", raising=False)
    assert _dashboard_action_transport() == "k8s"


def test_dashboard_action_transport_local(monkeypatch):
    from communication.infra.dashboard_actions import _dashboard_action_transport

    monkeypatch.setenv("UNITY_DASHBOARD_ACTION_TRANSPORT", "local")
    assert _dashboard_action_transport() == "local"
