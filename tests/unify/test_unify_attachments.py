"""
Tests for Unify message attachment support.

These tests verify behavior of attachment upload and message handling.
"""

import io
import json
import os
import sys
import pytest
from unittest.mock import MagicMock, patch

# =============================================================================
# MODULE-LEVEL MOCKS - Applied before any adapters imports
# =============================================================================

# Set required environment variables BEFORE imports
os.environ["GCP_SA_KEY"] = '{"type": "service_account", "project_id": "test"}'
os.environ["ORCHESTRA_ADMIN_KEY"] = "test-admin-key"
os.environ["GCP_PROJECT_ID"] = "test-project"
os.environ["ORCHESTRA_URL"] = "http://localhost:8000"

# Mock external dependencies that may not be installed
# These mocks are set at module level so they're applied before imports
_mock_livekit = MagicMock()
_mock_livekit.api = MagicMock()
sys.modules["livekit"] = _mock_livekit
sys.modules["livekit.api"] = _mock_livekit.api


# =============================================================================
# TEST FIXTURES
# =============================================================================


@pytest.fixture(scope="module")
def app_module():
    """
    Import the app module once per test module with mocked external services.
    This avoids re-importing for every test which is slow.
    """
    # Clear any cached adapters modules to force reimport with mocks
    for mod in list(sys.modules.keys()):
        if mod.startswith("adapters"):
            del sys.modules[mod]

    # Import with mocks in place
    from adapters import main

    return main


@pytest.fixture
def mock_gcs():
    """Mock GCS storage client and blob operations."""
    mock_blob = MagicMock()
    mock_blob.generate_signed_url.return_value = "https://storage.googleapis.com/test-bucket/signed-url?token=abc"
    mock_blob.upload_from_string = MagicMock()
    mock_blob.name = "test-path/file.txt"

    mock_bucket = MagicMock()
    mock_bucket.blob.return_value = mock_blob

    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    return mock_client, mock_bucket, mock_blob


@pytest.fixture
def mock_pubsub():
    """Mock Pub/Sub publisher client."""
    mock_future = MagicMock()
    mock_future.result.return_value = "test-message-id"

    mock_publisher = MagicMock()
    mock_publisher.topic_path.return_value = "projects/test/topics/unity-test-assistant"
    mock_publisher.publish.return_value = mock_future

    return mock_publisher


@pytest.fixture
def mock_webhook_context():
    """Mock the build_webhook_context helper."""
    return {
        "assistant": {"assistant_id": "test-assistant", "user_id": 12345},
        "contacts": [{"contact_id": 1, "first_name": "Test"}],
        "is_job_running": True,
    }


@pytest.fixture
def mock_get_assistant():
    """Mock the get_assistant helper to return user_id."""
    return {
        "assistant_id": "test-assistant",
        "user_id": 12345,
        "api_key": "test-api-key",
        "user_name": "Test User",
        "assistant_first_name": "Test",
        "assistant_surname": "Assistant",
    }


@pytest.fixture
def client(app_module, mock_gcs, mock_pubsub, mock_webhook_context, mock_get_assistant):
    """Create test client with mocked external services."""
    from fastapi.testclient import TestClient

    storage_client, mock_bucket, mock_blob = mock_gcs

    # Patch at the module level where the functions are used
    with (
        patch.object(app_module.storage, "Client", return_value=storage_client),
        patch.object(app_module.pubsub_v1, "PublisherClient", return_value=mock_pubsub),
        patch.object(app_module, "get_assistant", return_value=mock_get_assistant),
        patch.object(
            app_module.Credentials,
            "from_service_account_info",
            return_value=MagicMock(),
        ),
        patch.object(
            app_module,
            "build_webhook_context",
            return_value=mock_webhook_context,
        ),
    ):
        test_client = TestClient(app_module.app)
        test_client.headers["Authorization"] = "Bearer test-admin-key"

        # Attach mocks for assertions in tests
        test_client._mock_storage = storage_client
        test_client._mock_bucket = mock_bucket
        test_client._mock_blob = mock_blob
        test_client._mock_pubsub = mock_pubsub

        yield test_client


# =============================================================================
# CURRENT BEHAVIOR TESTS - These should pass with existing implementation
# =============================================================================


class TestAttachmentUploadCurrentBehavior:
    """Tests for current /unify/attachment endpoint behavior."""

    def test_upload_returns_id_filename_url(self, client):
        """Upload response includes id, filename, and url."""
        files = {"file": ("report.pdf", io.BytesIO(b"PDF content"), "application/pdf")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        data = response.json()

        # Current implementation returns these fields
        assert "id" in data
        assert "filename" in data
        assert "url" in data
        assert len(data["id"]) == 36  # UUID format

    def test_upload_preserves_filename(self, client):
        """Uploaded file retains its original filename."""
        files = {"file": ("my_document.pdf", io.BytesIO(b"content"), "application/pdf")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        assert response.json()["filename"] == "my_document.pdf"

    def test_upload_rejects_file_over_25mb(self, client):
        """Files larger than 25MB are rejected."""
        large_content = b"x" * (26 * 1024 * 1024)
        files = {"file": ("large.zip", io.BytesIO(large_content), "application/zip")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 400
        assert "25" in response.json()["error"]

    def test_upload_accepts_file_at_25mb(self, client):
        """Files exactly at 25MB are accepted."""
        content = b"x" * (25 * 1024 * 1024)
        files = {"file": ("at_limit.zip", io.BytesIO(content), "application/zip")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200

    def test_upload_sanitizes_path_traversal(self, client):
        """Path traversal attempts are sanitized."""
        files = {"file": ("../../../etc/passwd", io.BytesIO(b"x"), "text/plain")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        filename = response.json()["filename"]
        assert ".." not in filename
        assert "/" not in filename

    def test_upload_requires_authorization(self, client):
        """Requests without valid auth are rejected."""
        client.headers["Authorization"] = "Bearer wrong-key"
        files = {"file": ("test.txt", io.BytesIO(b"content"), "text/plain")}

        response = client.post("/unify/attachment", files=files)

        assert response.status_code == 401

    def test_upload_generates_unique_ids(self, client):
        """Each upload gets a unique ID."""
        ids = set()
        for i in range(3):
            files = {"file": (f"file{i}.txt", io.BytesIO(b"content"), "text/plain")}
            response = client.post(
                "/unify/attachment",
                files=files,
                data={"assistant_id": "test-assistant"},
            )
            assert response.status_code == 200
            ids.add(response.json()["id"])

        assert len(ids) == 3  # All unique


class TestMessageWithAttachmentsCurrentBehavior:
    """Tests for current /unify/message endpoint behavior with attachments."""

    def test_message_without_attachments(self, client):
        """Messages without attachments work normally."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Hello!",
            },
        )

        assert response.status_code == 200

        # Verify PubSub message
        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert published["thread"] == "unify_message"
        assert published["event"]["body"] == "Hello!"
        assert published["event"]["attachments"] == []

    def test_message_with_attachment(self, client):
        """Attachments are forwarded in PubSub message."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "See attached",
                "attachments": [
                    {"id": "abc-123", "filename": "doc.pdf", "url": "https://url"},
                ],
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert len(published["event"]["attachments"]) == 1
        assert published["event"]["attachments"][0]["filename"] == "doc.pdf"

    def test_message_with_multiple_attachments(self, client):
        """Multiple attachments are all forwarded."""
        attachments = [
            {"id": f"id-{i}", "filename": f"file{i}.pdf", "url": f"https://url{i}"}
            for i in range(5)
        ]

        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Multiple files",
                "attachments": attachments,
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert len(published["event"]["attachments"]) == 5

    def test_message_filters_invalid_attachments(self, client):
        """Invalid attachment objects are filtered out."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Test",
                "attachments": [
                    {"id": "valid", "filename": "good.pdf", "url": "https://url"},
                    {"id": "no-url", "filename": "bad.pdf"},  # Missing url
                    {"filename": "no-id.pdf", "url": "https://x"},  # Missing id
                    "not-an-object",
                    None,
                ],
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert len(published["event"]["attachments"]) == 1
        assert published["event"]["attachments"][0]["id"] == "valid"

    def test_message_requires_assistant_id(self, client):
        """assistant_id is required."""
        response = client.post(
            "/unify/message",
            json={"contact_id": 1, "body": "Hello"},
        )
        assert response.status_code == 400

    def test_message_requires_contact_id(self, client):
        """contact_id is required."""
        response = client.post(
            "/unify/message",
            json={"assistant_id": "test-assistant", "body": "Hello"},
        )
        assert response.status_code == 400

    def test_message_requires_authorization(self, client):
        """Requests without valid auth are rejected."""
        client.headers["Authorization"] = "Bearer wrong-key"
        response = client.post(
            "/unify/message",
            json={"assistant_id": "test", "contact_id": 1, "body": "Hi"},
        )
        assert response.status_code == 401


class TestEndToEndFlow:
    """Test upload then message flow."""

    def test_upload_then_send_message(self, client):
        """Complete flow: upload file, then send message with it."""
        # Step 1: Upload
        files = {"file": ("contract.pdf", io.BytesIO(b"PDF"), "application/pdf")}
        upload_resp = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )
        assert upload_resp.status_code == 200
        attachment = upload_resp.json()

        # Step 2: Send message with attachment
        msg_resp = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Please review",
                "attachments": [attachment],
            },
        )
        assert msg_resp.status_code == 200

        # Verify attachment was forwarded
        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert len(published["event"]["attachments"]) == 1
        assert published["event"]["attachments"][0]["filename"] == "contract.pdf"


# =============================================================================
# NEW BEHAVIOR TESTS - Expected to fail until features are implemented
# =============================================================================


class TestAttachmentUploadNewBehavior:
    """Tests for /unify/attachment new features.
    
    These features are now implemented:
    - gs_url in response (permanent URL)
    - content_type in response
    - size_bytes in response
    - File type validation (blocklist/allowlist)
    - User-scoped GCS paths
    """

    def test_upload_uses_user_id_in_path(self, client):
        """GCS path uses user_id from assistant lookup."""
        files = {"file": ("doc.pdf", io.BytesIO(b"content"), "application/pdf")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        data = response.json()
        
        # gs_url should contain user_id (12345 from mock) not assistant_id
        assert "gs_url" in data
        assert "/12345/" in data["gs_url"], f"Expected user_id in path, got: {data['gs_url']}"

    def test_upload_returns_gs_url(self, client):
        """Upload response includes permanent gs:// URL."""
        files = {"file": ("doc.pdf", io.BytesIO(b"content"), "application/pdf")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "gs_url" in data
        assert data["gs_url"].startswith("gs://")

    def test_upload_returns_content_type(self, client):
        """Upload response includes content_type."""
        files = {"file": ("doc.pdf", io.BytesIO(b"content"), "application/pdf")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "content_type" in data
        assert data["content_type"] == "application/pdf"

    def test_upload_returns_size_bytes(self, client):
        """Upload response includes file size in bytes."""
        content = b"x" * 1024  # 1KB
        files = {"file": ("doc.pdf", io.BytesIO(content), "application/pdf")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "size_bytes" in data
        assert data["size_bytes"] == 1024

    def test_upload_blocks_executable_files(self, client):
        """Executable file types are blocked."""
        blocked_files = [
            ("malware.exe", "application/x-msdownload"),
            ("script.bat", "application/x-msdos-program"),
            ("shell.sh", "application/x-sh"),
            ("powershell.ps1", "application/octet-stream"),
        ]

        for filename, mime in blocked_files:
            files = {"file": (filename, io.BytesIO(b"malicious"), mime)}
            response = client.post(
                "/unify/attachment",
                files=files,
                data={"assistant_id": "test-assistant"},
            )
            assert response.status_code == 400, f"{filename} should be blocked"
            assert "blocked" in response.json()["error"].lower() or "not allowed" in response.json()["error"].lower()

    def test_upload_blocks_unknown_file_types(self, client):
        """Unknown file types not in allowlist are blocked."""
        files = {"file": ("data.xyz", io.BytesIO(b"content"), "application/x-unknown")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 400


class TestMessageNewBehavior:
    """Tests for /unify/message new features.
    
    These features are now implemented:
    - Attachment count limit (max 10)
    - Full attachment metadata in PubSub (gs_url, content_type, size_bytes)
    """

    def test_message_rejects_more_than_10_attachments(self, client):
        """Messages with more than 10 attachments are rejected."""
        attachments = [
            {"id": f"id-{i}", "filename": f"file{i}.pdf", "url": f"https://url{i}"}
            for i in range(11)  # 11 attachments
        ]

        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Too many attachments",
                "attachments": attachments,
            },
        )

        assert response.status_code == 400
        assert "10" in response.text or "maximum" in response.text.lower()

    def test_message_includes_gs_url_in_pubsub(self, client):
        """PubSub message includes gs_url for each attachment."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "With gs_url",
                "attachments": [
                    {
                        "id": "att-1",
                        "filename": "doc.pdf",
                        "url": "https://signed-url",
                        "gs_url": "gs://bucket/path/doc.pdf",
                        "content_type": "application/pdf",
                        "size_bytes": 1024,
                    },
                ],
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        att = published["event"]["attachments"][0]
        
        # New fields should be preserved
        assert "gs_url" in att
        assert att["gs_url"] == "gs://bucket/path/doc.pdf"
        assert "content_type" in att
        assert "size_bytes" in att


# =============================================================================
# EDGE CASES - Should work with current implementation
# =============================================================================


class TestEdgeCases:
    """Edge cases that should work with current implementation."""

    def test_empty_body_with_attachment(self, client):
        """Message with empty body but attachments is valid."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "",
                "attachments": [
                    {"id": "att-1", "filename": "doc.pdf", "url": "https://url"},
                ],
            },
        )
        assert response.status_code == 200

    def test_null_attachments_field(self, client):
        """Null attachments field is handled as empty list."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "No attachments",
                "attachments": None,
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert published["event"]["attachments"] == []

    def test_missing_attachments_field(self, client):
        """Missing attachments field defaults to empty list."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "No attachments key",
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert published["event"]["attachments"] == []

    def test_filename_with_multiple_dots(self, client):
        """Filenames with multiple dots are preserved."""
        files = {"file": ("report.2026.01.final.pdf", io.BytesIO(b"x"), "application/pdf")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        assert response.json()["filename"] == "report.2026.01.final.pdf"

    def test_unicode_in_message_body(self, client):
        """Unicode content in message body is preserved."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Hello 你好 مرحبا 👋",
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert "你好" in published["event"]["body"]
        assert "👋" in published["event"]["body"]

    def test_attachment_order_preserved(self, client):
        """Attachments maintain their order."""
        attachments = [
            {"id": "first", "filename": "1.pdf", "url": "https://1"},
            {"id": "second", "filename": "2.pdf", "url": "https://2"},
            {"id": "third", "filename": "3.pdf", "url": "https://3"},
        ]

        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "Ordered",
                "attachments": attachments,
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        ids = [a["id"] for a in published["event"]["attachments"]]
        assert ids == ["first", "second", "third"]

    def test_upload_without_assistant_id(self, client):
        """Upload works without explicit assistant_id (uses default)."""
        files = {"file": ("doc.pdf", io.BytesIO(b"content"), "application/pdf")}

        response = client.post("/unify/attachment", files=files)

        assert response.status_code == 200
        assert "id" in response.json()

    def test_backslash_path_traversal(self, client):
        """Windows-style path traversal is sanitized."""
        files = {"file": ("..\\..\\windows\\system.txt", io.BytesIO(b"x"), "text/plain")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200
        filename = response.json()["filename"]
        assert ".." not in filename
        assert "\\" not in filename


# =============================================================================
# STRESS TESTS - Verify behavior at limits
# =============================================================================


class TestStressBehavior:
    """Test behavior at boundaries and under stress conditions."""

    def test_upload_at_exact_size_boundary(self, client):
        """Test precisely at the 25MB boundary."""
        # Exactly 25MB
        content = b"x" * (25 * 1024 * 1024)
        files = {"file": ("exact_25mb.zip", io.BytesIO(content), "application/zip")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 200

    def test_upload_one_byte_over_limit(self, client):
        """Test one byte over 25MB limit."""
        # 25MB + 1 byte
        content = b"x" * (25 * 1024 * 1024 + 1)
        files = {"file": ("over_limit.zip", io.BytesIO(content), "application/zip")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        assert response.status_code == 400

    def test_many_sequential_uploads(self, client):
        """Multiple sequential uploads all get unique IDs."""
        ids = set()
        for i in range(10):
            files = {"file": (f"file{i}.txt", io.BytesIO(b"content"), "text/plain")}
            response = client.post(
                "/unify/attachment",
                files=files,
                data={"assistant_id": "test-assistant"},
            )
            assert response.status_code == 200
            ids.add(response.json()["id"])

        # All IDs should be unique
        assert len(ids) == 10

    def test_same_content_different_filenames(self, client):
        """Same content with different filenames gets different IDs."""
        content = b"identical content"
        ids = []

        for name in ["v1.txt", "v2.txt", "v3.txt"]:
            files = {"file": (name, io.BytesIO(content), "text/plain")}
            response = client.post(
                "/unify/attachment",
                files=files,
                data={"assistant_id": "test-assistant"},
            )
            assert response.status_code == 200
            ids.append(response.json()["id"])

        # All should be unique even with same content
        assert len(set(ids)) == 3

    def test_message_with_empty_attachment_list(self, client):
        """Empty attachment list is valid."""
        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": "No attachments",
                "attachments": [],
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert published["event"]["attachments"] == []

    def test_long_message_body_with_attachments(self, client):
        """Long message body with attachments works."""
        long_body = "x" * 50000  # 50KB of text

        response = client.post(
            "/unify/message",
            json={
                "assistant_id": "test-assistant",
                "contact_id": 1,
                "body": long_body,
                "attachments": [
                    {"id": "att-1", "filename": "doc.pdf", "url": "https://url"},
                ],
            },
        )

        assert response.status_code == 200

        call_args = client._mock_pubsub.publish.call_args
        published = json.loads(call_args[0][1].decode("utf-8"))
        assert len(published["event"]["body"]) == 50000
        assert len(published["event"]["attachments"]) == 1

    def test_attachment_with_very_long_filename(self, client):
        """Very long filenames are handled."""
        long_filename = "a" * 200 + ".pdf"
        files = {"file": (long_filename, io.BytesIO(b"content"), "application/pdf")}

        response = client.post(
            "/unify/attachment",
            files=files,
            data={"assistant_id": "test-assistant"},
        )

        # Should succeed (may truncate internally)
        assert response.status_code == 200

    def test_attachment_with_special_mime_types(self, client):
        """Various allowed MIME types are handled."""
        mime_types = [
            ("doc.pdf", "application/pdf"),
            ("image.png", "image/png"),
            ("data.json", "application/json"),
            ("spreadsheet.csv", "text/csv"),
            ("archive.zip", "application/zip"),
        ]

        for filename, mime in mime_types:
            files = {"file": (filename, io.BytesIO(b"content"), mime)}
            response = client.post(
                "/unify/attachment",
                files=files,
                data={"assistant_id": "test-assistant"},
            )
            assert response.status_code == 200, f"Failed for {mime}"
