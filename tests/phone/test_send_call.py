"""Unit tests for the /phone/send-call endpoint.

Verifies that the SIP URI is built from the room_name parameter (not from
the phone number), matching the canonical unity_{assistant_id}_{medium}
format that LiveKit dispatch rules route on.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ENV = {
    "LIVEKIT_SIP_URI": "test.sip.livekit.cloud",
    "UNITY_COMMS_URL": "https://comms.example.com",
    "ORCHESTRA_ADMIN_KEY": "test-admin-key",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "test-token",
}


@pytest.fixture
def mock_twilio():
    mock_client = MagicMock()
    mock_call = MagicMock()
    mock_call.sid = "CA_test_call_sid"
    mock_client.calls.create.return_value = mock_call
    with patch(
        "communication.phone.views.get_twilio_client",
        return_value=mock_client,
    ):
        yield mock_client


@pytest.fixture
def client():
    with patch.dict(os.environ, ENV, clear=False):
        from communication.main import app

        return TestClient(app, raise_server_exceptions=False)


class TestSendCall:

    def test_sip_uri_uses_room_name(self, client, mock_twilio):
        """The SIP URI must encode the room name, not the phone number."""
        resp = client.post(
            "/phone/send-call",
            json={
                "From": "+15550100006",
                "To": "+19206146850",
                "room_name": "unity_568_phone",
            },
            headers={"Authorization": "Bearer test-admin-key"},
        )

        assert resp.status_code == 200
        call_kwargs = mock_twilio.calls.create.call_args
        sip_to = call_kwargs.kwargs.get("to") or call_kwargs[1].get("to")
        assert (
            sip_to == "sip:unity_568_phone@test.sip.livekit.cloud"
        ), f"Expected room-name-based SIP URI, got: {sip_to}"

    def test_twiml_url_contains_phone_number(self, client, mock_twilio):
        """The TwiML URL must still reference the recipient's phone number."""
        resp = client.post(
            "/phone/send-call",
            json={
                "From": "+15550100006",
                "To": "+19206146850",
                "room_name": "unity_568_phone",
            },
            headers={"Authorization": "Bearer test-admin-key"},
        )

        assert resp.status_code == 200
        call_kwargs = mock_twilio.calls.create.call_args
        twiml_url = call_kwargs.kwargs.get("url") or call_kwargs[1].get("url")
        assert "phone_number=+19206146850" in twiml_url
        assert "comms.example.com/phone/twiml" in twiml_url

    def test_returns_call_sid(self, client, mock_twilio):
        resp = client.post(
            "/phone/send-call",
            json={
                "From": "+15550100006",
                "To": "+19206146850",
                "room_name": "unity_568_phone",
            },
            headers={"Authorization": "Bearer test-admin-key"},
        )

        data = resp.json()
        assert data["success"] is True
        assert data["call_sid"] == "CA_test_call_sid"

    def test_sip_uri_never_contains_plus(self, client, mock_twilio):
        """Regression: old code put +{phone} in the SIP URI user part."""
        resp = client.post(
            "/phone/send-call",
            json={
                "From": "+447427857991",
                "To": "+442012345678",
                "room_name": "unity_42_phone",
            },
            headers={"Authorization": "Bearer test-admin-key"},
        )

        assert resp.status_code == 200
        call_kwargs = mock_twilio.calls.create.call_args
        sip_to = call_kwargs.kwargs.get("to") or call_kwargs[1].get("to")
        assert sip_to.startswith("sip:unity_42_phone@")
        assert "+" not in sip_to
