"""
Tests for Gmail send endpoint with attachment support.

These tests verify that the /gmail/send endpoint correctly handles:
- Sending emails without attachments (existing functionality)
- Sending emails with attachments (new functionality)
- Error handling for invalid attachment data
"""

import base64
import os
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_gmail_service():
    """Mock the Gmail API service."""
    mock_service = MagicMock()
    mock_service.users().messages().send().execute.return_value = {
        "id": "test_message_id_123",
    }
    return mock_service


@pytest.fixture
def client(mock_gmail_service):
    """Create a test client with mocked Gmail service and auth."""
    # Set required environment variables for the app
    os.environ.setdefault("GCP_SA_KEY", "{}")
    os.environ.setdefault("ORCHESTRA_ADMIN_KEY", "test-admin-key")

    with patch(
        "communication.gmail.views.get_gmail_service",
        return_value=mock_gmail_service,
    ):
        from communication.main import app

        test_client = TestClient(app)
        # Add auth header to all requests
        test_client.headers["Authorization"] = "Bearer test-admin-key"
        yield test_client


class TestSendEmailWithoutAttachment:
    """Tests for sending emails without attachments."""

    def test_send_basic_email(self, client, mock_gmail_service):
        """Send a basic email without attachment."""
        response = client.post(
            "/gmail/send",
            json={
                "from": "sender@example.com",
                "to": "recipient@example.com",
                "subject": "Test Subject",
                "body": "Test body content",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["id"] == "test_message_id_123"

    def test_send_email_with_reply_to(self, client, mock_gmail_service):
        """Send an email as a reply (threading)."""
        response = client.post(
            "/gmail/send",
            json={
                "from": "sender@example.com",
                "to": "recipient@example.com",
                "subject": "Re: Test Subject",
                "body": "Reply content",
                "in_reply_to": "<original-message-id@example.com>",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


class TestSendEmailWithAttachment:
    """Tests for sending emails with attachments."""

    def test_send_email_with_attachment(self, client, mock_gmail_service):
        """Send an email with a file attachment."""
        # Create test file content
        file_content = b"This is test file content"
        content_base64 = base64.b64encode(file_content).decode("utf-8")

        response = client.post(
            "/gmail/send",
            json={
                "from": "sender@example.com",
                "to": "recipient@example.com",
                "subject": "Email with attachment",
                "body": "Please see attached file",
                "attachment": {
                    "filename": "test_document.txt",
                    "content_base64": content_base64,
                },
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["id"] == "test_message_id_123"

        # Verify send was called (mock may be reused across tests)
        assert mock_gmail_service.users().messages().send.called

    def test_send_email_with_pdf_attachment(self, client, mock_gmail_service):
        """Send an email with a PDF attachment."""
        # Simulate PDF content (just some bytes for testing)
        pdf_content = b"%PDF-1.4 fake pdf content"
        content_base64 = base64.b64encode(pdf_content).decode("utf-8")

        response = client.post(
            "/gmail/send",
            json={
                "from": "sender@example.com",
                "to": "recipient@example.com",
                "subject": "Report attached",
                "body": "Here is the quarterly report",
                "attachment": {
                    "filename": "quarterly_report.pdf",
                    "content_base64": content_base64,
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["success"] is True

    def test_send_email_with_attachment_and_reply(self, client, mock_gmail_service):
        """Send a reply email with an attachment."""
        file_content = b"attachment content"
        content_base64 = base64.b64encode(file_content).decode("utf-8")

        response = client.post(
            "/gmail/send",
            json={
                "from": "sender@example.com",
                "to": "recipient@example.com",
                "subject": "Re: Document request",
                "body": "Here is the file you requested",
                "in_reply_to": "<message-id@example.com>",
                "attachment": {
                    "filename": "requested_file.docx",
                    "content_base64": content_base64,
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["success"] is True


class TestAttachmentErrorHandling:
    """Tests for attachment error handling."""

    def test_invalid_base64_content(self, client, mock_gmail_service):
        """Return error for invalid base64 content."""
        response = client.post(
            "/gmail/send",
            json={
                "from": "sender@example.com",
                "to": "recipient@example.com",
                "subject": "Test",
                "body": "Test body",
                "attachment": {
                    "filename": "test.txt",
                    "content_base64": "not-valid-base64!!!",
                },
            },
        )

        assert response.status_code == 400
        assert "Failed to attach file" in response.json()["detail"]

    def test_attachment_without_filename(self, client, mock_gmail_service):
        """Handle attachment without filename (uses default)."""
        file_content = b"content"
        content_base64 = base64.b64encode(file_content).decode("utf-8")

        response = client.post(
            "/gmail/send",
            json={
                "from": "sender@example.com",
                "to": "recipient@example.com",
                "subject": "Test",
                "body": "Test body",
                "attachment": {
                    "content_base64": content_base64,
                    # No filename - should use default
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["success"] is True


class TestRequestValidation:
    """Tests for request validation."""

    def test_missing_required_fields(self, client):
        """Return error when required fields are missing."""
        response = client.post(
            "/gmail/send",
            json={
                "subject": "Test",
                # Missing 'from', 'to', 'body'
            },
        )

        assert response.status_code == 400
        assert "Missing required fields" in response.json()["detail"]

    def test_missing_body(self, client):
        """Return error when body is missing."""
        response = client.post(
            "/gmail/send",
            json={
                "from": "sender@example.com",
                "to": "recipient@example.com",
                "subject": "Test",
                # Missing 'body'
            },
        )

        assert response.status_code == 400
