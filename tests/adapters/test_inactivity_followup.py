"""Unit tests for the assistant inactivity-followup adapters endpoint."""

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


def _payload() -> dict:
    return {"assistant_id": "assistant-123"}


def test_inactivity_followup_attaches_wake_reason_for_cold_start():
    """Cold-start deliveries should pass the wake reason to dispatch_unity_start_intent."""

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
            "/assistant/inactivity-followup",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "attached_to_startup"
    assert body["assistant_id"] == "assistant-123"
    mock_publish.assert_not_called()
    assert mock_dispatch.call_args.args[1] == "api_message"
    assert mock_dispatch.call_args.kwargs["wake_reasons"] == [
        {"type": "inactivity_followup"},
    ]


def test_inactivity_followup_publishes_event_for_running_session():
    """Active sessions should receive a live `inactivity_followup` system event."""

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
            "/assistant/inactivity-followup",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published_to_active_session"
    mock_publish.assert_called_once()
    assert mock_publish.call_args.kwargs["event_type"] == "inactivity_followup"
    assert mock_publish.call_args.kwargs["extra_event_fields"] == {
        "type": "inactivity_followup"
    }


def test_inactivity_followup_local_runtime_publishes_directly():
    """Local-runtime assistants skip the cold-pod path and publish directly."""

    client = TestClient(app)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=_assistant_data()),
        patch("adapters.main.uses_local_unity_runtime", return_value=True),
        patch(
            "adapters.main.dispatch_unity_start_intent",
        ) as mock_dispatch,
        patch("adapters.main._publish_unity_system_event") as mock_publish,
    ):
        response = client.post(
            "/assistant/inactivity-followup",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "published_local"
    assert body["assistant_id"] == "assistant-123"
    mock_dispatch.assert_not_called()
    mock_publish.assert_called_once()
    assert mock_publish.call_args.kwargs["event_type"] == "inactivity_followup"


def test_inactivity_followup_skips_deleted_assistant():
    """Missing assistants should ack without retries."""

    client = TestClient(app)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value={"assistant_id": None}),
    ):
        response = client.post(
            "/assistant/inactivity-followup",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "assistant_not_found"
