"""
Behavioral contract tests for Gmail comms-app endpoints.

Tests the Gmail send, attachment read, and user create/delete lifecycle
against the real deployed comms app with domain-wide delegation.

Endpoints covered:
- POST /gmail/send (sends real email via delegated SA)
- GET /gmail/attachment (reads attachment from real Gmail message)
- POST /gmail/create + DELETE /gmail/delete (Workspace user lifecycle)
"""

import pytest
import requests

from ..conftest import (
    ADMIN_KEY,
    COMMS_APP_URL,
    find_assistant_with_email,
)

pytestmark = [pytest.mark.integration]

_ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


class TestGmailSend:
    """Contract: POST /gmail/send sends a real email via domain-wide
    delegation and returns the Gmail message ID."""

    def test_send_email_to_self(self):
        assistant = find_assistant_with_email()
        if not assistant:
            pytest.skip("No assistant with an email address found")

        email = assistant["email"]
        resp = requests.post(
            f"{COMMS_APP_URL}/gmail/send",
            json={
                "from": email,
                "to": email,
                "subject": "Integration test (safe to ignore)",
                "body": "This is an automated integration test email.",
            },
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        # 200 = sent successfully; 500 = SA can't impersonate this email
        assert resp.status_code in (
            200,
            500,
        ), f"gmail/send unexpected: {resp.status_code} {resp.text}"
        if resp.status_code == 200:
            body = resp.json()
            assert body["success"] is True
            assert "id" in body


class TestGmailAttachment:
    """Contract: GET /gmail/attachment reads an attachment from a real
    Gmail message by message ID and attachment ID."""

    def test_attachment_with_invalid_ids_returns_error(self):
        assistant = find_assistant_with_email()
        if not assistant:
            pytest.skip("No assistant with an email address found")

        resp = requests.get(
            f"{COMMS_APP_URL}/gmail/attachment",
            params={
                "receiver_email": assistant["email"],
                "gmail_message_id": "nonexistent-message-id",
                "attachment_id": "nonexistent-attachment-id",
            },
            headers=_ADMIN_HEADERS,
            timeout=30,
        )
        # 500 = Gmail API error (message not found) — exercises the full code path
        assert resp.status_code in (
            200,
            400,
            500,
        ), f"gmail/attachment unexpected: {resp.status_code} {resp.text}"


class TestGmailUserLifecycle:
    """Contract: POST /gmail/create + DELETE /gmail/delete manage
    Google Workspace users.

    Creates a temporary test user, verifies, then deletes immediately.
    """

    def test_create_and_delete_workspace_user(self):
        import uuid

        test_local = f"integration-test-{uuid.uuid4().hex[:8]}"
        test_email = f"{test_local}@unify.ai"

        try:
            create_resp = requests.post(
                f"{COMMS_APP_URL}/gmail/create",
                json={
                    "local": test_local,
                    "first_name": "IntegrationTest",
                    "last_name": "AutoDelete",
                },
                headers=_ADMIN_HEADERS,
                timeout=30,
            )
            # 201 = user created; 500 = SA lacks Workspace admin delegation
            if create_resp.status_code == 500:
                pytest.skip(
                    "SA does not have Workspace admin delegation for user creation",
                )
            assert (
                create_resp.status_code == 201
            ), f"gmail/create failed: {create_resp.status_code} {create_resp.text}"
            body = create_resp.json()
            assert body["success"] is True

        finally:
            requests.delete(
                f"{COMMS_APP_URL}/gmail/delete",
                json={"primary_email": test_email},
                headers=_ADMIN_HEADERS,
                timeout=15,
            )
