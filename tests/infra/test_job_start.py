"""
Unit tests for the /infra/job/start endpoint.

These tests verify:
- Demo mode flag is properly accepted and passed in job data
- Job data is correctly structured for Pub/Sub message
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


def _create_job_start_payload(demo_mode="false"):
    """Create a valid payload for /job/start endpoint."""
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
        "demo_mode": demo_mode,
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
    def test_job_start_accepts_demo_mode_true(
        self, mock_publisher_class, mock_creds, client
    ):
        """Verify endpoint accepts demo_mode='true' and includes it in job data."""
        # Setup mocks
        mock_creds.return_value = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher_class.return_value = mock_publisher
        mock_future = MagicMock()
        mock_future.result.return_value = "test-message-id"
        mock_publisher.publish.return_value = mock_future
        mock_publisher.topic_path.return_value = "projects/test/topics/unity-startup"

        # Make request
        payload = _create_job_start_payload(demo_mode="true")
        response = client.post("/infra/job/start", data=payload)

        assert response.status_code == 200

        # Verify the published message contains demo_mode=True
        mock_publisher.publish.assert_called_once()
        call_args = mock_publisher.publish.call_args
        published_data = json.loads(
            call_args.kwargs.get("data") or call_args[1]["data"]
        )

        assert (
            published_data["event"]["demo_mode"] is True
        ), f"Expected demo_mode=True in job data, got {published_data['event'].get('demo_mode')}"

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict(
        "os.environ",
        {
            "GCP_SA_KEY": '{"type": "service_account", "project_id": "test", "private_key_id": "1", "private_key": "key", "client_email": "service-account@example.iam.gserviceaccount.com", "client_id": "1", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}',
        },
    )
    def test_job_start_accepts_demo_mode_false(
        self, mock_publisher_class, mock_creds, client
    ):
        """Verify endpoint accepts demo_mode='false' and includes it in job data."""
        mock_creds.return_value = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher_class.return_value = mock_publisher
        mock_future = MagicMock()
        mock_future.result.return_value = "test-message-id"
        mock_publisher.publish.return_value = mock_future
        mock_publisher.topic_path.return_value = "projects/test/topics/unity-startup"

        payload = _create_job_start_payload(demo_mode="false")
        response = client.post("/infra/job/start", data=payload)

        assert response.status_code == 200

        call_args = mock_publisher.publish.call_args
        published_data = json.loads(
            call_args.kwargs.get("data") or call_args[1]["data"]
        )

        assert (
            published_data["event"]["demo_mode"] is False
        ), f"Expected demo_mode=False in job data, got {published_data['event'].get('demo_mode')}"

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict(
        "os.environ",
        {
            "GCP_SA_KEY": '{"type": "service_account", "project_id": "test", "private_key_id": "1", "private_key": "key", "client_email": "service-account@example.iam.gserviceaccount.com", "client_id": "1", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}',
        },
    )
    def test_job_start_defaults_demo_mode_to_false(
        self, mock_publisher_class, mock_creds, client
    ):
        """Verify demo_mode defaults to false when not provided."""
        mock_creds.return_value = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher_class.return_value = mock_publisher
        mock_future = MagicMock()
        mock_future.result.return_value = "test-message-id"
        mock_publisher.publish.return_value = mock_future
        mock_publisher.topic_path.return_value = "projects/test/topics/unity-startup"

        # Create payload without demo_mode
        payload = _create_job_start_payload()
        del payload["demo_mode"]

        response = client.post("/infra/job/start", data=payload)

        assert response.status_code == 200

        call_args = mock_publisher.publish.call_args
        published_data = json.loads(
            call_args.kwargs.get("data") or call_args[1]["data"]
        )

        assert (
            published_data["event"]["demo_mode"] is False
        ), f"Expected demo_mode=False as default, got {published_data['event'].get('demo_mode')}"

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict(
        "os.environ",
        {
            "GCP_SA_KEY": '{"type": "service_account", "project_id": "test", "private_key_id": "1", "private_key": "key", "client_email": "service-account@example.iam.gserviceaccount.com", "client_id": "1", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}',
        },
    )
    def test_job_start_normalizes_demo_mode_case(
        self, mock_publisher_class, mock_creds, client
    ):
        """Verify demo_mode handles case-insensitive 'TRUE' and 'True'."""
        mock_creds.return_value = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher_class.return_value = mock_publisher
        mock_future = MagicMock()
        mock_future.result.return_value = "test-message-id"
        mock_publisher.publish.return_value = mock_future
        mock_publisher.topic_path.return_value = "projects/test/topics/unity-startup"

        for demo_value in ["TRUE", "True", "TrUe"]:
            mock_publisher.reset_mock()

            payload = _create_job_start_payload(demo_mode=demo_value)
            response = client.post("/infra/job/start", data=payload)

            assert response.status_code == 200

            call_args = mock_publisher.publish.call_args
            published_data = json.loads(
                call_args.kwargs.get("data") or call_args[1]["data"]
            )

            assert (
                published_data["event"]["demo_mode"] is True
            ), f"Expected demo_mode=True for input '{demo_value}'"

    @patch("communication.infra.views.Credentials.from_service_account_info")
    @patch("communication.infra.views.pubsub_v1.PublisherClient")
    @patch.dict(
        "os.environ",
        {
            "GCP_SA_KEY": '{"type": "service_account", "project_id": "test", "private_key_id": "1", "private_key": "key", "client_email": "service-account@example.iam.gserviceaccount.com", "client_id": "1", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}',
        },
    )
    def test_job_start_includes_all_required_fields_in_event(
        self, mock_publisher_class, mock_creds, client
    ):
        """Verify all required fields are included in the published event."""
        mock_creds.return_value = MagicMock()
        mock_publisher = MagicMock()
        mock_publisher_class.return_value = mock_publisher
        mock_future = MagicMock()
        mock_future.result.return_value = "test-message-id"
        mock_publisher.publish.return_value = mock_future
        mock_publisher.topic_path.return_value = "projects/test/topics/unity-startup"

        payload = _create_job_start_payload(demo_mode="true")
        response = client.post("/infra/job/start", data=payload)

        assert response.status_code == 200

        call_args = mock_publisher.publish.call_args
        published_data = json.loads(
            call_args.kwargs.get("data") or call_args[1]["data"]
        )

        event = published_data["event"]

        # Verify essential fields
        assert event["assistant_id"] == "12345"
        assert event["user_id"] == "user-123"
        assert event["medium"] == "phone"
        assert event["demo_mode"] is True
        assert "thread" in published_data
        assert published_data["thread"] == "startup"
