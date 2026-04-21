"""Tests for the DELETE /gmail/watch endpoint.

Stopping the Gmail push-notification watch is called by Orchestra's
disconnect flow before the BYOD token is revoked, so the endpoint must
tolerate ``HttpError`` cases (e.g. the watch already being absent) while
surfacing real failures to the caller.
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from common.settings import SETTINGS


def _http_error(status_code: int) -> HttpError:
    """Construct an ``HttpError`` whose ``resp.status`` matches ``status_code``."""
    resp = MagicMock()
    resp.status = status_code
    resp.reason = "error"
    return HttpError(resp=resp, content=b"")


@pytest.fixture
def mock_gmail_service():
    """Mock the Gmail API service."""
    mock_service = MagicMock()
    mock_service.users().stop().execute.return_value = {}
    return mock_service


@pytest.fixture
def client(mock_gmail_service):
    """Create a test client with mocked Gmail service and auth."""
    os.environ.setdefault("GCP_SA_KEY", "{}")

    with (
        patch(
            "communication.gmail.views.get_gmail_service_async",
            new_callable=AsyncMock,
            return_value=mock_gmail_service,
        ),
        patch.object(SETTINGS, "orchestra_admin_key", "test-admin-key"),
    ):
        from communication.main import app

        test_client = TestClient(app)
        test_client.headers["Authorization"] = "Bearer test-admin-key"
        yield test_client


class TestDeleteGmailWatch:
    """Tests for DELETE /gmail/watch."""

    def test_stop_watch_success(self, client, mock_gmail_service):
        """Stop the Gmail watch for the given primary email."""
        response = client.request(
            "DELETE",
            "/gmail/watch",
            json={"primary_email": "user@byod.com"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["primary_email"] == "user@byod.com"
        assert data.get("already_absent") is not True

        mock_gmail_service.users().stop.assert_called_with(userId="me")

    def test_missing_primary_email_returns_400(self, client):
        """Return 400 when primary_email is missing."""
        response = client.request("DELETE", "/gmail/watch", json={})

        assert response.status_code == 400
        assert response.json()["detail"] == "Missing primary_email"

    def test_stop_watch_404_treated_as_already_absent(
        self,
        client,
        mock_gmail_service,
    ):
        """A 404 from Gmail's stop call is a benign no-op."""
        mock_gmail_service.users().stop().execute.side_effect = _http_error(404)

        response = client.request(
            "DELETE",
            "/gmail/watch",
            json={"primary_email": "user@byod.com"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["already_absent"] is True
        assert data["primary_email"] == "user@byod.com"

    def test_stop_watch_500_bubbles_up(self, client, mock_gmail_service):
        """Non-404 Gmail API errors surface as 500 to the caller."""
        mock_gmail_service.users().stop().execute.side_effect = _http_error(500)

        response = client.request(
            "DELETE",
            "/gmail/watch",
            json={"primary_email": "user@byod.com"},
        )

        assert response.status_code == 500


class TestDeleteGmailWatchAuth:
    """Tests for admin-key auth on DELETE /gmail/watch."""

    def test_missing_auth_returns_unauthorized(self, client):
        """The Gmail router requires an admin bearer token."""
        client.headers.pop("Authorization", None)

        response = client.request(
            "DELETE",
            "/gmail/watch",
            json={"primary_email": "user@byod.com"},
        )

        assert response.status_code in (401, 403)
