"""Unit tests for idempotent contact deletion endpoints."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

GCP_SA_KEY_JSON = json.dumps(
    {
        "type": "service_account",
        "project_id": "test",
        "private_key_id": "1",
        "private_key": "key",
        "client_email": "service-account@example.iam.gserviceaccount.com",
        "client_id": "1",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    },
)


@pytest.fixture
def phone_client():
    """Create a FastAPI client for the authenticated phone router."""
    from communication.phone.views import auth_router

    app = FastAPI()
    app.include_router(auth_router, prefix="/phone")
    return TestClient(app)


@pytest.fixture
def gmail_client(monkeypatch):
    """Create a FastAPI client for the Gmail router with test credentials."""
    monkeypatch.setenv("GCP_SA_KEY", GCP_SA_KEY_JSON)

    import communication.gmail.views as gmail_views

    app = FastAPI()
    app.include_router(gmail_views.router, prefix="/gmail")
    return TestClient(app), gmail_views


def _google_http_error(status: int) -> HttpError:
    """Construct a minimal Google API error for tests."""
    response = MagicMock()
    response.status = status
    return HttpError(resp=response, content=b"{}")


def test_delete_email_user_succeeds_when_workspace_user_already_absent(gmail_client):
    client, gmail_views = gmail_client

    mock_service = MagicMock()
    mock_service.users.return_value.delete.return_value.execute.side_effect = (
        _google_http_error(404)
    )

    with patch.object(gmail_views, "get_admin_service", return_value=mock_service):
        response = client.request(
            "DELETE",
            "/gmail/delete",
            json={"primary_email": "gone@unify.ai"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "deleted": False,
        "already_absent": True,
        "message": "User gone@unify.ai already absent.",
    }


def test_delete_phone_number_succeeds_when_number_absent_but_sip_trunk_exists(
    phone_client,
):
    twilio_client = MagicMock()
    twilio_client.incoming_phone_numbers.list.return_value = []

    livekit = MagicMock()
    trunk = MagicMock()
    trunk.name = "Unity_15551234567"
    trunk.sip_trunk_id = "sip-trunk-123"
    livekit.sip.list_sip_inbound_trunk = AsyncMock(
        return_value=MagicMock(
            items=[trunk],
        ),
    )
    livekit.sip.delete_sip_trunk = AsyncMock()
    livekit.aclose = AsyncMock()

    with (
        patch(
            "communication.phone.views.get_twilio_client",
            return_value=twilio_client,
        ),
        patch(
            "communication.phone.views.get_livekit_api",
            return_value=livekit,
        ),
    ):
        response = phone_client.request(
            "DELETE",
            "/phone/delete",
            json={"PhoneNumber": "+15551234567"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "sid": None,
        "deleted": True,
        "already_absent": True,
        "sip_trunk_deleted": True,
    }
    livekit.sip.delete_sip_trunk.assert_awaited_once()
    livekit.aclose.assert_awaited_once()
