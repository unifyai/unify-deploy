"""
Tests for Unify message attachment support.

These tests verify that:
- /unify/attachment endpoint uploads files and returns signed URLs
- /unify/message endpoint accepts and forwards attachments to Pub/Sub
- Error handling works correctly for invalid inputs

These are unit tests that mock external dependencies (GCS, Pub/Sub).
"""

import io
import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch


# Mock external dependencies before any imports
@pytest.fixture(scope="module", autouse=True)
def setup_mocks():
    """Set up module-level mocks before importing the app."""
    # Set required environment variables
    os.environ["GCP_SA_KEY"] = '{"type": "service_account", "project_id": "test"}'
    os.environ["ORCHESTRA_ADMIN_KEY"] = "test-admin-key"
    os.environ["PROJECT_ID"] = "test-project"
    os.environ["ORCHESTRA_URL"] = "http://localhost:8000"


@pytest.fixture
def mock_storage_client():
    """Mock the GCS storage client."""
    mock_blob = MagicMock()
    mock_blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed-url"
    mock_blob.upload_from_string = MagicMock()

    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob

    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    return mock_client, mock_bucket, mock_blob


@pytest.fixture
def mock_pubsub_client():
    """Mock the Pub/Sub publisher client."""
    mock_future = MagicMock()
    mock_future.result.return_value = "test-message-id"

    mock_client = MagicMock()
    mock_client.topic_path.return_value = "projects/test/topics/unity-test"
    mock_client.publish.return_value = mock_future

    return mock_client


@pytest.fixture
def client(mock_storage_client, mock_pubsub_client):
    """Create a test client with mocked dependencies."""
    storage_client, mock_bucket, mock_blob = mock_storage_client

    # Mock all external dependencies at the module level
    with patch.dict(
        "sys.modules", {
            "azure": MagicMock(),
            "azure.core": MagicMock(),
            "azure.core.credentials": MagicMock(),
            "azure.identity": MagicMock(),
            "twilio": MagicMock(),
            "twilio.twiml": MagicMock(),
            "twilio.twiml.messaging_response": MagicMock(),
            "twilio.twiml.voice_response": MagicMock(),
            "googleapiclient": MagicMock(),
            "googleapiclient.discovery": MagicMock(),
        },
    ):
        # Force reimport to pick up mocks
        if "adapters.main" in sys.modules:
            del sys.modules["adapters.main"]
        if "adapters.helpers" in sys.modules:
            del sys.modules["adapters.helpers"]

        # Mock helpers module to avoid complex dependency chains
        mock_helpers = MagicMock()
        mock_helpers.build_webhook_context.return_value = {
            "assistant": {"assistant_id": "test-assistant"},
            "contacts": [{"contact_id": 1, "first_name": "Test"}],
            "is_job_running": True,
        }
        mock_helpers.STAGING = False
        mock_helpers.ORCHESTRA_URL = "http://localhost:8000"
        mock_helpers.COMMS_URL = "http://localhost:8001"

        with patch.dict("sys.modules", {"adapters.helpers": mock_helpers}):
            from adapters.main import app
            from fastapi.testclient import TestClient

            # Now patch the specific clients inside the app module
            with patch.object(
                sys.modules["adapters.main"].storage, "Client", return_value=storage_client,
            ), patch.object(
                sys.modules["adapters.main"].pubsub_v1, "PublisherClient", return_value=mock_pubsub_client,
            ), patch.object(
                sys.modules["adapters.main"].Credentials, "from_service_account_info", return_value=MagicMock(),
            ):
                test_client = TestClient(app)
                test_client.headers["Authorization"] = "Bearer test-admin-key"

                # Attach mocks for assertions
                test_client._mock_storage = storage_client
                test_client._mock_bucket = mock_bucket
                test_client._mock_blob = mock_blob
                test_client._mock_pubsub = mock_pubsub_client

                yield test_client


class TestUnifyAttachmentUpload:
    """Tests for /unify/attachment endpoint."""

    def test_upload_file_success(self, client):
        """Upload a file and receive attachment details."""
        file_content = b"This is test file content"
        files = {"file": ("test_document.txt", io.BytesIO(file_content), "text/plain")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        data = response.json()

        # Verify response structure
        assert "id" in data
        assert data["filename"] == "test_document.txt"
        assert data["url"] == "https://storage.googleapis.com/signed-url"

        # Verify UUID format for id
        assert len(data["id"]) == 36  # UUID length with hyphens

        # Verify blob was uploaded
        client._mock_blob.upload_from_string.assert_called_once()

        # Verify signed URL was generated
        client._mock_blob.generate_signed_url.assert_called_once()

    def test_upload_pdf_file(self, client):
        """Upload a PDF file."""
        pdf_content = b"%PDF-1.4 fake pdf content"
        files = {"file": ("report.pdf", io.BytesIO(pdf_content), "application/pdf")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["filename"] == "report.pdf"

    def test_upload_without_assistant_id(self, client):
        """Upload with default assistant_id."""
        file_content = b"content"
        files = {"file": ("file.txt", io.BytesIO(file_content), "text/plain")}

        response = client.post("/unify/attachment", files=files)

        assert response.status_code == 200
        # Should use "unknown" as default assistant_id

    def test_upload_file_too_large(self, client):
        """Reject files exceeding 25MB limit."""
        # Create content larger than 25MB
        large_content = b"x" * (26 * 1024 * 1024)
        files = {"file": ("large_file.bin", io.BytesIO(large_content), "application/octet-stream")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 400
        data = response.json()
        assert "too large" in data["error"].lower()
        assert "25MB" in data["error"]

    def test_upload_unauthorized(self, client):
        """Reject unauthorized requests."""
        client.headers["Authorization"] = "Bearer wrong-key"
        file_content = b"content"
        files = {"file": ("file.txt", io.BytesIO(file_content), "text/plain")}

        response = client.post("/unify/attachment", files=files)

        assert response.status_code == 401

    def test_upload_sanitizes_filename(self, client):
        """Ensure filename is sanitized (no path traversal)."""
        file_content = b"content"
        # Try to include path in filename
        files = {"file": ("../../../etc/passwd", io.BytesIO(file_content), "text/plain")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        data = response.json()
        # Should only have the basename
        assert data["filename"] == "passwd"
        assert ".." not in data["filename"]


class TestUnifyMessageWithAttachments:
    """Tests for /unify/message endpoint with attachments."""

    def test_message_without_attachments(self, client):
        """Send a message without attachments (existing behavior)."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Hello, world!",
            },
        )

        assert response.status_code == 200

        # Verify Pub/Sub was called
        client._mock_pubsub.publish.assert_called_once()

        # Get the published message
        call_args = client._mock_pubsub.publish.call_args
        published_data = json.loads(call_args[0][1].decode("utf-8"))

        assert published_data["thread"] == "unify_message"
        assert published_data["event"]["body"] == "Hello, world!"
        assert published_data["event"]["attachments"] == []

    def test_message_with_single_attachment(self, client):
        """Send a message with one attachment."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Here is the document",
                "attachments": [
                    {
                        "id": "abc-123",
                        "filename": "document.pdf",
                        "url": "https://storage.googleapis.com/bucket/path/document.pdf",
                    },
                ],
            },
        )

        assert response.status_code == 200

        # Verify the attachment was included in Pub/Sub message
        call_args = client._mock_pubsub.publish.call_args
        published_data = json.loads(call_args[0][1].decode("utf-8"))

        assert len(published_data["event"]["attachments"]) == 1
        attachment = published_data["event"]["attachments"][0]
        assert attachment["id"] == "abc-123"
        assert attachment["filename"] == "document.pdf"
        assert "storage.googleapis.com" in attachment["url"]

    def test_message_with_multiple_attachments(self, client):
        """Send a message with multiple attachments."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Multiple files attached",
                "attachments": [
                    {"id": "id-1", "filename": "file1.pdf", "url": "https://url1"},
                    {"id": "id-2", "filename": "file2.docx", "url": "https://url2"},
                    {"id": "id-3", "filename": "image.png", "url": "https://url3"},
                ],
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published_data = json.loads(call_args[0][1].decode("utf-8"))

        assert len(published_data["event"]["attachments"]) == 3

    def test_message_filters_invalid_attachments(self, client):
        """Invalid attachments are filtered out."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Test",
                "attachments": [
                    {"id": "valid", "filename": "file.txt", "url": "https://url"},
                    {"id": "missing-url", "filename": "bad.txt"},  # Missing url
                    {"filename": "no-id.txt", "url": "https://url"},  # Missing id
                    "not-an-object",  # Not a dict
                    None,  # Null
                ],
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published_data = json.loads(call_args[0][1].decode("utf-8"))

        # Only the valid attachment should be included
        assert len(published_data["event"]["attachments"]) == 1
        assert published_data["event"]["attachments"][0]["id"] == "valid"

    def test_message_unauthorized(self, client):
        """Reject unauthorized requests."""
        client.headers["Authorization"] = "Bearer wrong-key"

        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "body": "Hello",
            },
        )

        assert response.status_code == 401

    def test_message_missing_assistant_id(self, client):
        """Require assistant_id."""
        response = client.post(
            "/unify/message",
            json={"body": "Hello"},
        )

        assert response.status_code == 400


class TestEndToEndFlow:
    """Test the complete upload-then-message flow."""

    def test_upload_then_send_message(self, client):
        """Upload an attachment, then send a message with it."""
        # Step 1: Upload attachment
        file_content = b"Important document content"
        files = {"file": ("contract.pdf", io.BytesIO(file_content), "application/pdf")}

        upload_response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert upload_response.status_code == 200
        attachment = upload_response.json()

        # Step 2: Send message with the uploaded attachment
        message_response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Please review the attached contract",
                "attachments": [attachment],
            },
        )

        assert message_response.status_code == 200

        # Verify the attachment was forwarded correctly
        call_args = client._mock_pubsub.publish.call_args
        published_data = json.loads(call_args[0][1].decode("utf-8"))

        assert published_data["event"]["body"] == "Please review the attached contract"
        assert len(published_data["event"]["attachments"]) == 1
        assert published_data["event"]["attachments"][0]["filename"] == "contract.pdf"
