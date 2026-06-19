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
        },
        run_key="dashboard_action:assistant-123:tile-abc:refresh_metrics:abc",
        job_name="droid-dashboard-action-abc",
    )

    assert env["ASSISTANT_IS_COORDINATOR"] == "False"
