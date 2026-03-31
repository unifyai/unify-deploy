"""Tests for POST /api/message adapter endpoint."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ["GCP_SA_KEY"] = '{"type": "service_account", "project_id": "test"}'
os.environ["ORCHESTRA_ADMIN_KEY"] = "test-admin-key"
os.environ["GCP_PROJECT_ID"] = "test-project"
os.environ["ORCHESTRA_URL"] = "http://localhost:8000"

_mock_livekit = MagicMock()
_mock_livekit.api = MagicMock()
_mock_livekit.protocol = MagicMock()
_mock_livekit.protocol.sip = MagicMock()
sys.modules["livekit"] = _mock_livekit
sys.modules["livekit.api"] = _mock_livekit.api
sys.modules["livekit.protocol"] = _mock_livekit.protocol
sys.modules["livekit.protocol.sip"] = _mock_livekit.protocol.sip


@pytest.fixture(scope="module")
def app_module():
    for mod in list(sys.modules.keys()):
        if mod.startswith("adapters"):
            del sys.modules[mod]
    from adapters import main

    return main


@pytest.fixture
def mock_pubsub():
    mock_future = MagicMock()
    mock_future.result.return_value = "test-message-id"
    mock_publisher = MagicMock()
    mock_publisher.topic_path.return_value = "projects/test/topics/unity-test-assistant"
    mock_publisher.publish.return_value = mock_future
    return mock_publisher


@pytest.fixture
def mock_webhook_context():
    return {
        "assistant": {"assistant_id": "test-assistant", "user_id": 12345},
        "contacts": [{"contact_id": 1, "first_name": "Test"}],
        "is_job_running": True,
    }


@pytest.fixture
def client(app_module, mock_pubsub, mock_webhook_context):
    from fastapi.testclient import TestClient

    with (
        patch.object(
            app_module,
            "get_pubsub_client",
            return_value=mock_pubsub,
        ),
        patch.object(
            app_module,
            "build_webhook_context",
            return_value=mock_webhook_context,
        ),
    ):
        test_client = TestClient(app_module.app)
        test_client.headers["Authorization"] = "Bearer test-admin-key"
        test_client._mock_pubsub = mock_pubsub
        yield test_client


class TestApiMessage:

    def test_success(self, client):
        response = client.post(
            "/api/message",
            json={
                "assistant_id": "test-assistant",
                "api_message_id": "msg-uuid-123",
                "body": "Hello from API",
            },
        )
        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert published["thread"] == "api_message"
        assert published["event"]["api_message_id"] == "msg-uuid-123"
        assert published["event"]["body"] == "Hello from API"
        assert published["event"]["contact_id"] == 1
        assert published["event"]["assistant_id"] == "test-assistant"
        assert "publish_timestamp" in published

    def test_empty_body_allowed(self, client):
        response = client.post(
            "/api/message",
            json={
                "assistant_id": "test-assistant",
                "api_message_id": "msg-uuid-456",
                "body": "",
            },
        )
        assert response.status_code == 200

    def test_missing_assistant_id(self, client):
        response = client.post(
            "/api/message",
            json={"api_message_id": "msg-uuid", "body": "Hello"},
        )
        assert response.status_code == 400
        assert "assistant_id" in response.text

    def test_missing_api_message_id(self, client):
        response = client.post(
            "/api/message",
            json={"assistant_id": "test-assistant", "body": "Hello"},
        )
        assert response.status_code == 400
        assert "api_message_id" in response.text

    def test_requires_auth(self, client):
        client.headers["Authorization"] = "Bearer wrong-key"
        response = client.post(
            "/api/message",
            json={
                "assistant_id": "test-assistant",
                "api_message_id": "msg-uuid",
                "body": "Hello",
            },
        )
        assert response.status_code in (401, 403)

    def test_no_auth_header(self, client):
        del client.headers["Authorization"]
        response = client.post(
            "/api/message",
            json={
                "assistant_id": "test-assistant",
                "api_message_id": "msg-uuid",
                "body": "Hello",
            },
        )
        assert response.status_code == 401

    # ─── Attachments and Tags ───

    def test_tags_included_in_pubsub(self, client):
        response = client.post(
            "/api/message",
            json={
                "assistant_id": "test-assistant",
                "api_message_id": "msg-tags-001",
                "body": "Tagged message",
                "tags": ["source:slack", "channel:#general"],
            },
        )
        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert published["event"]["tags"] == ["source:slack", "channel:#general"]

    def test_attachments_included_in_pubsub(self, client):
        attachment = {
            "id": "att-001",
            "filename": "report.pdf",
            "gs_url": "gs://bucket/path/report.pdf",
            "content_type": "application/pdf",
            "size_bytes": 12345,
        }
        response = client.post(
            "/api/message",
            json={
                "assistant_id": "test-assistant",
                "api_message_id": "msg-att-001",
                "body": "See attached",
                "attachments": [attachment],
            },
        )
        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        atts = published["event"]["attachments"]
        assert len(atts) == 1
        assert atts[0]["id"] == "att-001"
        assert atts[0]["filename"] == "report.pdf"
        assert atts[0]["gs_url"] == "gs://bucket/path/report.pdf"

    def test_invalid_attachments_skipped(self, client):
        response = client.post(
            "/api/message",
            json={
                "assistant_id": "test-assistant",
                "api_message_id": "msg-att-bad",
                "body": "Bad attachments",
                "attachments": [
                    {"id": "att-ok", "filename": "ok.txt", "gs_url": "gs://bucket/ok"},
                    {"bad": "data"},
                    "not-a-dict",
                ],
            },
        )
        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert len(published["event"]["attachments"]) == 1

    def test_too_many_attachments_rejected(self, client):
        attachments = [
            {"id": f"att-{i}", "filename": f"f{i}.txt", "gs_url": f"gs://bucket/{i}"}
            for i in range(11)
        ]
        response = client.post(
            "/api/message",
            json={
                "assistant_id": "test-assistant",
                "api_message_id": "msg-att-many",
                "body": "Too many",
                "attachments": attachments,
            },
        )
        assert response.status_code == 400
        assert "10" in response.text

    def test_empty_tags_and_attachments_omitted_from_event(self, client):
        response = client.post(
            "/api/message",
            json={
                "assistant_id": "test-assistant",
                "api_message_id": "msg-empty-extras",
                "body": "No extras",
                "tags": [],
                "attachments": [],
            },
        )
        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert "attachments" not in published["event"]
        assert "tags" not in published["event"]

    def test_no_tags_or_attachments_backward_compatible(self, client):
        response = client.post(
            "/api/message",
            json={
                "assistant_id": "test-assistant",
                "api_message_id": "msg-compat",
                "body": "Old-style message",
            },
        )
        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert "attachments" not in published["event"]
        assert "tags" not in published["event"]

    def test_calls_build_webhook_context(self, app_module, mock_pubsub):
        from fastapi.testclient import TestClient

        mock_ctx = {
            "assistant": {"assistant_id": "test-assistant", "user_id": 12345},
            "contacts": [],
            "is_job_running": False,
        }
        with (
            patch.object(
                app_module,
                "get_pubsub_client",
                return_value=mock_pubsub,
            ),
            patch.object(
                app_module,
                "build_webhook_context",
                return_value=mock_ctx,
            ) as mock_bwc,
        ):
            tc = TestClient(app_module.app)
            tc.headers["Authorization"] = "Bearer test-admin-key"
            tc.post(
                "/api/message",
                json={
                    "assistant_id": "42",
                    "api_message_id": "msg-1",
                    "body": "Hi",
                },
            )
            mock_bwc.assert_called_once_with(
                channel="api_message",
                destination="",
                sender="",
                assistant_id="42",
                validate_contact=False,
                ensure_job=True,
            )
