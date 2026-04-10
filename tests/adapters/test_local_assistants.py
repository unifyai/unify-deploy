"""Regression tests for local-assistant startup behavior in adapters."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["GCP_SA_KEY"] = '{"type": "service_account", "project_id": "test"}'
os.environ["ORCHESTRA_ADMIN_KEY"] = "test-admin-key"
os.environ["GCP_PROJECT_ID"] = "test-project"
os.environ["ORCHESTRA_URL"] = "http://localhost:8000"
os.environ.setdefault("OUTLOOK_WEBHOOK_SECRET", "test-outlook-secret")
os.environ.setdefault("TEAMS_WEBHOOK_SECRET", "test-teams-secret")

_mock_livekit = MagicMock()
_mock_livekit.api = MagicMock()
_mock_livekit.protocol = MagicMock()
_mock_livekit.protocol.sip = MagicMock()
sys.modules["livekit"] = _mock_livekit
sys.modules["livekit.api"] = _mock_livekit.api
sys.modules["livekit.protocol"] = _mock_livekit.protocol
sys.modules["livekit.protocol.sip"] = _mock_livekit.protocol.sip


class _GraphResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response_payload: dict):
        self._response_payload = response_payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *_args, **_kwargs):
        return _GraphResponse(self._response_payload)


@pytest.fixture(scope="module")
def app_module():
    for mod in list(sys.modules.keys()):
        if mod.startswith("adapters"):
            del sys.modules[mod]
    from adapters import main

    return main


def _assistant_data(*, is_local: bool) -> dict:
    return {
        "assistant_id": "assistant-123",
        "user_id": "user-123",
        "api_key": "api-key",
        "user_number": "",
        "user_whatsapp_number": "",
        "user_email": "user@example.com",
        "assistant_email": "assistant@example.com",
        "assistant_first_name": "Local",
        "assistant_surname": "Assistant",
        "assistant_age": "25",
        "assistant_nationality": "US",
        "assistant_about": "Local runtime test assistant",
        "assistant_timezone": "UTC",
        "assistant_number": "",
        "assistant_whatsapp_number": "",
        "voice_provider": "elevenlabs",
        "voice_id": "voice-123",
        "desktop_mode": "ubuntu",
        "user_desktop_mode": "",
        "user_desktop_filesys_sync": False,
        "user_desktop_url": "",
        "demo_id": "",
        "team_ids": [],
        "org_id": "",
        "deploy_env": "staging",
        "is_local": is_local,
        "secrets": {"MICROSOFT_ACCESS_TOKEN": "test-token"},
    }


def _mock_pubsub():
    mock_future = MagicMock()
    mock_future.result.return_value = "test-message-id"
    mock_publisher = MagicMock()
    mock_publisher.topic_path.return_value = "projects/test/topics/unity-assistant-123"
    mock_publisher.publish.return_value = mock_future
    return mock_publisher


@pytest.mark.parametrize(("is_local", "should_start"), [(True, False), (False, True)])
def test_outlook_notification_respects_local_runtime(
    app_module,
    is_local,
    should_start,
):
    from fastapi.testclient import TestClient

    assistant_data = _assistant_data(is_local=is_local)

    with (
        patch.object(
            app_module,
            "build_webhook_context",
            return_value={"assistant": assistant_data},
        ),
        patch.object(
            app_module,
            "get_graph_client_from_token",
            return_value=MagicMock(),
        ),
        patch.object(
            app_module,
            "get_outlook_thread_id",
            new=AsyncMock(
                return_value=(
                    "conversation-123",
                    "email-123",
                    {"sender": "sender@example.com"},
                ),
            ),
        ),
        patch.object(
            app_module,
            "check_valid_contact",
            return_value=([{"contact_id": 1}], True),
        ),
        patch.object(app_module, "publish_outlook_thread_id") as mock_publish_thread,
        patch.object(app_module, "start_unity_job") as mock_start_unity_job,
    ):
        client = TestClient(app_module.app)
        response = client.post(
            "/email/outlook",
            json={
                "clientState": "test-outlook-secret::assistant@example.com",
                "resource": "/users/test/mailFolders/inbox/Messages/email-123",
            },
        )

    assert response.status_code == 200
    assert response.text == "OK"
    mock_publish_thread.assert_called_once()
    if should_start:
        mock_start_unity_job.assert_called_once_with(assistant_data, "email")
    else:
        mock_start_unity_job.assert_not_called()


@pytest.mark.parametrize(("is_local", "should_start"), [(True, False), (False, True)])
def test_teams_notification_respects_local_runtime(
    app_module,
    is_local,
    should_start,
):
    from fastapi.testclient import TestClient

    assistant_data = _assistant_data(is_local=is_local)
    mock_publisher = _mock_pubsub()
    message_payload = {
        "id": "message-123",
        "from": {
            "user": {
                "displayName": "Sender",
                "userPrincipalName": "sender@example.com",
                "id": "sender-123",
            },
        },
        "body": {"content": "Hello from Teams", "contentType": "text"},
        "createdDateTime": "2026-04-10T00:00:00Z",
        "subject": "",
    }

    with (
        patch.object(
            app_module,
            "build_webhook_context",
            return_value={"assistant": assistant_data},
        ),
        patch.object(
            app_module,
            "check_valid_contact",
            return_value=([{"contact_id": 1}], True),
        ),
        patch.object(app_module, "get_pubsub_client", return_value=mock_publisher),
        patch.object(app_module, "start_unity_job") as mock_start_unity_job,
        patch.object(
            app_module.httpx,
            "AsyncClient",
            return_value=_FakeAsyncClient(message_payload),
        ),
    ):
        client = TestClient(app_module.app)
        response = client.post(
            "/chat/teams",
            json={
                "clientState": "test-teams-secret::assistant@example.com",
                "resource": "/chats/chat-123/messages/message-123",
                "resourceData": {"id": "message-123", "chatId": "chat-123"},
            },
        )

    assert response.status_code == 200
    assert response.text == "OK"
    mock_publisher.publish.assert_called_once()
    if should_start:
        mock_start_unity_job.assert_called_once_with(assistant_data, "teams")
    else:
        mock_start_unity_job.assert_not_called()
