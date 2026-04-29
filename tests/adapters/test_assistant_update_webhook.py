"""Focused tests for assistant update event publishing."""

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from adapters.main import app
from common.settings import SETTINGS


class _PublishFuture:
    def result(self, timeout=None):
        return "message-123"


class _Publisher:
    def __init__(self):
        self.published: dict | None = None

    def topic_path(self, project_id: str, topic_name: str) -> str:
        return f"projects/{project_id}/topics/{topic_name}"

    def publish(self, topic_path: str, data: bytes, thread: str):
        self.published = {
            "topic_path": topic_path,
            "data": data,
            "thread": thread,
        }
        return _PublishFuture()


def _context(space_ids=None) -> dict:
    return {
        "assistant": {
            "assistant_id": "assistant-123",
            "api_key": "test-api-key",
            "user_id": "user-123",
            "user_first_name": "Test",
            "user_surname": "User",
            "user_email": "test@example.com",
            "user_number": "+1234567890",
            "user_whatsapp_number": "+1234567890",
            "assistant_first_name": "Test",
            "assistant_surname": "Assistant",
            "assistant_timezone": "UTC",
            "space_ids": space_ids or [],
        },
        "contacts": [],
        "is_valid_contact": True,
        "matched_contact": None,
        "is_job_running": False,
        "job_started": False,
    }


def _client() -> TestClient:
    return TestClient(app)


@patch.object(SETTINGS, "orchestra_admin_key", "test-key")
@patch("adapters.main.get_pubsub_client")
@patch("adapters.main.build_webhook_context")
def test_membership_update_publishes_space_ids_inside_event(
    mock_build_context,
    mock_get_pubsub_client,
):
    """Membership updates publish the runtime event without waking cold sessions."""

    publisher = _Publisher()
    mock_get_pubsub_client.return_value = publisher
    mock_build_context.return_value = _context(space_ids=[9])

    response = _client().post(
        "/assistant/update",
        data={
            "assistant_id": "assistant-123",
            "space_ids": "[3, 4]",
            "update_kind": "membership",
        },
        headers={"Authorization": "Bearer test-key"},
    )

    assert response.status_code == 200
    assert mock_build_context.call_args.kwargs["ensure_job"] is False
    published = json.loads(publisher.published["data"].decode("utf-8"))
    assert published["thread"] == "assistant_update"
    assert published["event"]["assistant_id"] == "assistant-123"
    assert published["event"]["space_ids"] == [3, 4]
    assert published["event"]["update_kind"] == "membership"


@patch.object(SETTINGS, "orchestra_admin_key", "test-key")
@patch("adapters.main.get_pubsub_client")
@patch("adapters.main.build_webhook_context")
def test_general_update_invokes_ensure_job(mock_build_context, mock_get_pubsub_client):
    """General assistant updates keep the existing wake behavior."""

    publisher = _Publisher()
    mock_get_pubsub_client.return_value = publisher
    mock_build_context.return_value = _context(space_ids=[5, 6])

    response = _client().post(
        "/assistant/update",
        data={"assistant_id": "assistant-123"},
        headers={"Authorization": "Bearer test-key"},
    )

    assert response.status_code == 200
    assert mock_build_context.call_args.kwargs["ensure_job"] is True
    published = json.loads(publisher.published["data"].decode("utf-8"))
    assert published["event"]["space_ids"] == [5, 6]
    assert published["event"]["update_kind"] == "general"


@patch.object(SETTINGS, "orchestra_admin_key", "test-key")
def test_invalid_update_kind_returns_400():
    """The update discriminator accepts only the runtime-supported values."""

    response = _client().post(
        "/assistant/update",
        data={"assistant_id": "assistant-123", "update_kind": "config"},
        headers={"Authorization": "Bearer test-key"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "update_kind must be 'general' or 'membership'"


@patch.object(SETTINGS, "orchestra_admin_key", "test-key")
@patch("adapters.main.build_webhook_context")
def test_invalid_space_ids_returns_400_before_wake(mock_build_context):
    """Malformed membership payloads fail before any startup side effect."""

    response = _client().post(
        "/assistant/update",
        data={"assistant_id": "assistant-123", "space_ids": '[1, "bad"]'},
        headers={"Authorization": "Bearer test-key"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "space_ids must be a list of integers"
    mock_build_context.assert_not_called()
