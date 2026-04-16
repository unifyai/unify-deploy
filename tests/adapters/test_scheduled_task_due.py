"""Unit tests for the scheduled task due adapters endpoint."""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from adapters.main import app


def _assistant_data() -> dict:
    return {
        "assistant_id": "assistant-123",
        "user_id": "user-123",
        "api_key": "user-api-key",
        "user_first_name": "Test",
        "user_surname": "User",
        "user_email": "user@example.com",
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
        "assistant_age": "25",
        "assistant_nationality": "US",
        "assistant_about": "Test assistant",
        "assistant_timezone": "UTC",
        "assistant_email": "assistant@example.com",
        "user_number": "+1234567890",
        "assistant_number": "+1987654321",
        "user_whatsapp_number": "+1234567890",
        "assistant_whatsapp_number": "+1987654321",
        "assistant_discord_bot_id": "",
        "voice_provider": "elevenlabs",
        "voice_id": "voice-123",
        "desktop_mode": "none",
        "user_desktop_mode": None,
        "user_desktop_filesys_sync": False,
        "user_desktop_url": None,
        "team_ids": [],
        "org_id": None,
        "deploy_env": "staging",
        "is_local": False,
    }


def _task_due_payload() -> dict:
    return {
        "assistant_id": "assistant-123",
        "task_id": 101,
        "source_task_log_id": 555,
        "activation_revision": "rev-123",
        "scheduled_for": "2026-04-10T09:00:00+00:00",
        "execution_mode": "live",
        "source_type": "scheduled",
        "task_label": "Morning briefing",
        "task_summary": "Prepare the morning update before the user checks in.",
        "visibility_policy": "silent_by_default",
        "recurrence_hint": "recurring",
    }


def test_scheduled_task_due_attaches_wake_reason_for_cold_start():
    """Cold-start deliveries should attach wake reasons without live publish."""

    client = TestClient(app)
    start_response = MagicMock(status_code=200)
    start_response.json.return_value = {
        "success": True,
        "activation_id": "activation-1",
        "reused_active_session": False,
        "active_session_already_running": False,
    }

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=_assistant_data()),
        patch("adapters.main.uses_local_unity_runtime", return_value=False),
        patch(
            "adapters.main.dispatch_unity_start_intent",
            return_value=start_response,
        ) as mock_dispatch,
        patch("adapters.main._publish_unity_system_event") as mock_publish,
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_task_due_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "attached_to_startup"
    mock_publish.assert_not_called()
    assert mock_dispatch.call_args.args[1] == "api_message"
    wake_reasons = mock_dispatch.call_args.kwargs["wake_reasons"]
    assert wake_reasons == [
        {
            "type": "task_due",
            "task_id": 101,
            "source_task_log_id": 555,
            "activation_revision": "rev-123",
            "scheduled_for": "2026-04-10T09:00:00+00:00",
            "execution_mode": "live",
            "source_type": "scheduled",
            "task_label": "Morning briefing",
            "task_summary": "Prepare the morning update before the user checks in.",
            "visibility_policy": "silent_by_default",
            "recurrence_hint": "recurring",
        },
    ]


def test_scheduled_task_due_publishes_event_for_running_session():
    """Active sessions should receive a live `task_due` system event."""

    client = TestClient(app)
    start_response = MagicMock(status_code=200)
    start_response.json.return_value = {
        "success": True,
        "activation_id": "activation-2",
        "reused_active_session": True,
        "active_session_already_running": True,
    }

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=_assistant_data()),
        patch("adapters.main.uses_local_unity_runtime", return_value=False),
        patch(
            "adapters.main.dispatch_unity_start_intent",
            return_value=start_response,
        ),
        patch("adapters.main._publish_unity_system_event") as mock_publish,
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_task_due_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published_to_active_session"
    mock_publish.assert_called_once()
    assert mock_publish.call_args.kwargs["event_type"] == "task_due"
    assert (
        mock_publish.call_args.kwargs["message"]
        == "Scheduled task 'Morning briefing' became due at 2026-04-10T09:00:00+00:00."
    )
    assert mock_publish.call_args.kwargs["extra_event_fields"]["task_id"] == 101
    assert (
        mock_publish.call_args.kwargs["extra_event_fields"]["task_label"]
        == "Morning briefing"
    )


def test_scheduled_task_due_skips_deleted_assistant():
    """Missing assistants should ack the delivery without retries."""

    client = TestClient(app)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value={"assistant_id": None}),
    ):
        response = client.post(
            "/scheduled/tasks/due",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_task_due_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "assistant_not_found"
