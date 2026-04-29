"""Tests for dashboard action offline runner payloads."""

from communication.infra.dashboard_actions import (
    DashboardActionDispatchRequest,
    _build_dashboard_action_env,
)


def test_dashboard_action_env_carries_space_ids_as_csv():
    """Dashboard action runs use the same membership env bridge as tasks."""

    request = DashboardActionDispatchRequest(
        assistant_id="assistant-123",
        tile_token="tile-abc",
        action_name="Refresh KPI",
    )

    env = _build_dashboard_action_env(
        request=request,
        action_metadata={"function_id": 777},
        assistant_data={
            "assistant_id": "assistant-123",
            "api_key": "test-api-key",
            "space_ids": [3, 4],
        },
        run_key="run-123",
        job_name="unity-dashboard-action-abc",
    )

    assert env["SPACE_IDS"] == "3,4"


def test_dashboard_action_env_uses_empty_space_ids_for_solo_assistant():
    """Solo dashboard action runs keep the env value present but empty."""

    request = DashboardActionDispatchRequest(
        assistant_id="assistant-123",
        tile_token="tile-abc",
        action_name="Refresh KPI",
    )

    env = _build_dashboard_action_env(
        request=request,
        action_metadata={"function_id": 777},
        assistant_data={
            "assistant_id": "assistant-123",
            "api_key": "test-api-key",
            "space_ids": [],
        },
        run_key="run-123",
        job_name="unity-dashboard-action-abc",
    )

    assert env["SPACE_IDS"] == ""
