"""Unit tests for the Coordinator delegation adapters endpoint."""

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
    return {
        "assistant_id": "assistant-123",
        "requested_by_assistant_id": "coordinator-456",
        "instruction": "Schedule the renewal summary tomorrow.",
        "intent": "schedule_task",
        "dedupe_key": "renewal-summary",
        "related_context": {"source": "coordinator"},
    }


def _wake_reason() -> dict:
    return {
        "type": "coordinator_delegate",
        "requested_by_assistant_id": "coordinator-456",
        "instruction": "Schedule the renewal summary tomorrow.",
        "intent": "schedule_task",
        "dedupe_key": "renewal-summary",
        "related_context": {"source": "coordinator"},
    }


def _async_delegation_receipt() -> dict:
    return {
        "accepted": True,
        "completion_status": "pending_async",
        "receipt_type": "async_delegation_receipt",
        "message": (
            "The colleague has been woken or notified with the assignment. "
            "This does not mean the colleague has already created durable artifacts "
            "or completed the work."
        ),
    }


def test_coordinator_delegate_attaches_wake_reason_for_cold_start():
    """Cold-start deliveries should pass the wake reason to start intent dispatch."""

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
            "/assistant/coordinator-delegate",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "success": True,
        "status": "attached_to_startup",
        "assistant_id": "assistant-123",
        "activation_id": "activation-1",
        **_async_delegation_receipt(),
    }
    mock_publish.assert_not_called()
    assert mock_dispatch.call_args.args[1] == "api_message"
    assert mock_dispatch.call_args.kwargs["wake_reasons"] == [_wake_reason()]


def test_coordinator_delegate_publishes_event_for_running_session():
    """Active sessions should receive a live `coordinator_delegate` system event."""

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
            "/assistant/coordinator-delegate",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "success": True,
        "status": "published_to_active_session",
        "assistant_id": "assistant-123",
        "activation_id": "activation-2",
        **_async_delegation_receipt(),
    }
    mock_publish.assert_called_once()
    assert mock_publish.call_args.kwargs["event_type"] == "coordinator_delegate"
    assert mock_publish.call_args.kwargs["extra_event_fields"] == _wake_reason()


def test_coordinator_delegate_local_runtime_publishes_directly():
    """Local-runtime assistants skip cold-pod dispatch and publish directly."""

    client = TestClient(app)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value=_assistant_data()),
        patch("adapters.main.uses_local_unity_runtime", return_value=True),
        patch("adapters.main.dispatch_unity_start_intent") as mock_dispatch,
        patch("adapters.main._publish_unity_system_event") as mock_publish,
    ):
        response = client.post(
            "/assistant/coordinator-delegate",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "success": True,
        "status": "published_local",
        "assistant_id": "assistant-123",
        **_async_delegation_receipt(),
    }
    mock_dispatch.assert_not_called()
    mock_publish.assert_called_once()
    assert mock_publish.call_args.kwargs["event_type"] == "coordinator_delegate"


def test_coordinator_delegate_skips_deleted_assistant():
    """Missing assistants should ack without retries."""

    client = TestClient(app)

    with (
        patch("adapters.main.SETTINGS.orchestra_admin_key", "test-admin-key"),
        patch("adapters.main.get_assistant", return_value={"assistant_id": None}),
    ):
        response = client.post(
            "/assistant/coordinator-delegate",
            headers={"Authorization": "Bearer test-admin-key"},
            json=_payload(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert body["reason"] == "assistant_not_found"
