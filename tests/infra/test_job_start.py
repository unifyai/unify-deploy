"""
Unit tests for the /infra/job/start endpoint.

These tests verify:
- Demo ID is properly accepted and passed in job data
- Job data is correctly structured for Pub/Sub message
- Unity receives demo_id (int or None) to derive demo_mode
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


# Create a test client with mocked dependencies
@pytest.fixture
def client():
    """Create a FastAPI test client for the infra router."""
    from fastapi import FastAPI
    from communication.infra.views import router

    app = FastAPI()
    app.include_router(router, prefix="/infra")
    return TestClient(app)


def _create_job_start_payload(demo_id=""):
    """Create a valid payload for /job/start endpoint.

    Args:
        demo_id: Demo assistant metadata ID as string.
                 Empty string for regular assistants, numeric string for demos.
    """
    return {
        "api_key": "test-api-key",
        "medium": "phone",
        "assistant_id": "12345",
        "user_id": "user-123",
        "user_name": "Test User",
        "user_email": "test@example.com",
        "assistant_name": "Test Assistant",
        "assistant_age": "25",
        "assistant_nationality": "US",
        "assistant_about": "A test assistant",
        "assistant_timezone": "UTC",
        "user_number": "+1234567890",
        "assistant_number": "+0987654321",
        "assistant_email": "assistant@example.com",
        "user_whatsapp_number": "+1234567890",
        "voice_provider": "elevenlabs",
        "voice_id": "voice-123",
        "voice_mode": "tts",
        "desktop_mode": "ubuntu",
        "desktop_url": "",
        "user_desktop_mode": "",
        "user_desktop_filesys_sync": "false",
        "user_desktop_url": "",
        "demo_id": demo_id,
    }


class TestJobStartEndpoint:
    """Tests for the /infra/job/start endpoint."""

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict(
        "os.environ",
        {
            "GCP_SA_KEY": '{"type": "service_account", "project_id": "test", "private_key_id": "1", "private_key": "key", "client_email": "service-account@example.iam.gserviceaccount.com", "client_id": "1", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}',
        },
    )
    def test_job_start_accepts_demo_id(self, mock_publisher_class, mock_creds, client):
        """Verify endpoint accepts demo_id and includes it as int in job data."""
        # Setup mocks
        mock_creds.return_value = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher_class.return_value = mock_publisher
        mock_future = MagicMock()
        mock_future.result.return_value = "test-message-id"
        mock_publisher.publish.return_value = mock_future
        mock_publisher.topic_path.return_value = "projects/test/topics/unity-startup"

        # Make request with demo_id
        payload = _create_job_start_payload(demo_id="42")
        response = client.post("/infra/job/start", data=payload)

        assert response.status_code == 200

        # Verify the published message contains demo_id as int
        mock_publisher.publish.assert_called_once()
        call_args = mock_publisher.publish.call_args
        published_data = json.loads(
            call_args.kwargs.get("data") or call_args[1]["data"],
        )

        assert (
            published_data["event"]["demo_id"] == 42
        ), f"Expected demo_id=42 in job data, got {published_data['event'].get('demo_id')}"

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict(
        "os.environ",
        {
            "GCP_SA_KEY": '{"type": "service_account", "project_id": "test", "private_key_id": "1", "private_key": "key", "client_email": "service-account@example.iam.gserviceaccount.com", "client_id": "1", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}',
        },
    )
    def test_job_start_demo_id_none_for_regular_assistant(
        self,
        mock_publisher_class,
        mock_creds,
        client,
    ):
        """Verify demo_id is None when empty string is passed (regular assistant)."""
        mock_creds.return_value = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher_class.return_value = mock_publisher
        mock_future = MagicMock()
        mock_future.result.return_value = "test-message-id"
        mock_publisher.publish.return_value = mock_future
        mock_publisher.topic_path.return_value = "projects/test/topics/unity-startup"

        payload = _create_job_start_payload(demo_id="")
        response = client.post("/infra/job/start", data=payload)

        assert response.status_code == 200

        call_args = mock_publisher.publish.call_args
        published_data = json.loads(
            call_args.kwargs.get("data") or call_args[1]["data"],
        )

        assert (
            published_data["event"]["demo_id"] is None
        ), f"Expected demo_id=None in job data, got {published_data['event'].get('demo_id')}"

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict(
        "os.environ",
        {
            "GCP_SA_KEY": '{"type": "service_account", "project_id": "test", "private_key_id": "1", "private_key": "key", "client_email": "service-account@example.iam.gserviceaccount.com", "client_id": "1", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}',
        },
    )
    def test_job_start_defaults_demo_id_to_none(
        self,
        mock_publisher_class,
        mock_creds,
        client,
    ):
        """Verify demo_id defaults to None when not provided."""
        mock_creds.return_value = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher_class.return_value = mock_publisher
        mock_future = MagicMock()
        mock_future.result.return_value = "test-message-id"
        mock_publisher.publish.return_value = mock_future
        mock_publisher.topic_path.return_value = "projects/test/topics/unity-startup"

        # Create payload without demo_id
        payload = _create_job_start_payload()
        del payload["demo_id"]

        response = client.post("/infra/job/start", data=payload)

        assert response.status_code == 200

        call_args = mock_publisher.publish.call_args
        published_data = json.loads(
            call_args.kwargs.get("data") or call_args[1]["data"],
        )

        assert (
            published_data["event"]["demo_id"] is None
        ), f"Expected demo_id=None as default, got {published_data['event'].get('demo_id')}"

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict(
        "os.environ",
        {
            "GCP_SA_KEY": '{"type": "service_account", "project_id": "test", "private_key_id": "1", "private_key": "key", "client_email": "service-account@example.iam.gserviceaccount.com", "client_id": "1", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}',
        },
    )
    def test_job_start_demo_id_with_large_id(
        self,
        mock_publisher_class,
        mock_creds,
        client,
    ):
        """Verify demo_id handles large numeric IDs correctly."""
        mock_creds.return_value = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher_class.return_value = mock_publisher
        mock_future = MagicMock()
        mock_future.result.return_value = "test-message-id"
        mock_publisher.publish.return_value = mock_future
        mock_publisher.topic_path.return_value = "projects/test/topics/unity-startup"

        large_demo_id = "123456789"
        payload = _create_job_start_payload(demo_id=large_demo_id)
        response = client.post("/infra/job/start", data=payload)

        assert response.status_code == 200

        call_args = mock_publisher.publish.call_args
        published_data = json.loads(
            call_args.kwargs.get("data") or call_args[1]["data"],
        )

        assert (
            published_data["event"]["demo_id"] == 123456789
        ), f"Expected demo_id=123456789 for input '{large_demo_id}'"

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict(
        "os.environ",
        {
            "GCP_SA_KEY": '{"type": "service_account", "project_id": "test", "private_key_id": "1", "private_key": "key", "client_email": "service-account@example.iam.gserviceaccount.com", "client_id": "1", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}',
        },
    )
    def test_job_start_includes_all_required_fields_in_event(
        self,
        mock_publisher_class,
        mock_creds,
        client,
    ):
        """Verify all required fields are included in the published event."""
        mock_creds.return_value = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher_class.return_value = mock_publisher
        mock_future = MagicMock()
        mock_future.result.return_value = "test-message-id"
        mock_publisher.publish.return_value = mock_future
        mock_publisher.topic_path.return_value = "projects/test/topics/unity-startup"

        payload = _create_job_start_payload(demo_id="99")
        response = client.post("/infra/job/start", data=payload)

        assert response.status_code == 200

        call_args = mock_publisher.publish.call_args
        published_data = json.loads(
            call_args.kwargs.get("data") or call_args[1]["data"],
        )

        event = published_data["event"]

        # Verify essential fields
        assert event["assistant_id"] == "12345"
        assert event["user_id"] == "user-123"
        assert event["medium"] == "phone"
        assert event["demo_id"] == 99
        assert "thread" in published_data
        assert published_data["thread"] == "startup"
