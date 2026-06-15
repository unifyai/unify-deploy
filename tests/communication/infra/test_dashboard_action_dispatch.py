"""Tests for dashboard action offline runner payloads."""

import json

from communication.infra.dashboard_actions import (
    DashboardActionDispatchRequest,
    _build_dashboard_action_env,
)


def test_dashboard_action_env_carries_team_ids_as_csv():
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
            "team_ids": [3, 4],
            "team_summaries": [
                {
                    "team_id": 3,
                    "name": "Ops",
                    "description": "Operations workspace for dashboard actions.",
                },
            ],
            "self_contact_id": 42,
            "boss_contact_id": 43,
        },
        run_key="run-123",
        job_name="unity-dashboard-action-abc",
    )

    assert env["TEAM_IDS"] == "3,4"
    assert json.loads(env["TEAM_SUMMARIES"]) == [
        {
            "team_id": 3,
            "name": "Ops",
            "description": "Operations workspace for dashboard actions.",
        },
    ]
    assert env["SELF_CONTACT_ID"] == "42"
    assert env["BOSS_CONTACT_ID"] == "43"


def test_dashboard_action_env_uses_empty_team_ids_for_solo_assistant():
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
            "team_ids": [],
            "self_contact_id": 42,
            "boss_contact_id": 43,
        },
        run_key="run-123",
        job_name="unity-dashboard-action-abc",
    )

    assert env["TEAM_IDS"] == ""
    assert env["TEAM_SUMMARIES"] == ""
