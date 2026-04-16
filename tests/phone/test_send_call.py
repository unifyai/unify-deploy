"""Unit tests for the /phone/send-call endpoint.

Verifies that the SIP URI uses the E.164 phone number as the user part
(LiveKit matches it against the inbound trunk's ``numbers`` field),
while a per-trunk dispatch rule routes the SIP participant into the
correct room.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from common.settings import SETTINGS

ENV = {
    "LIVEKIT_SIP_URI": "test.sip.livekit.cloud",
    "UNITY_COMMS_URL": "https://comms.example.com",
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
    with (
        patch.dict(os.environ, ENV, clear=False),
        patch.object(SETTINGS, "orchestra_admin_key", "test-admin-key"),
        patch.object(SETTINGS, "comms_url", "https://comms.example.com"),
    ):
        from communication.main import app

        yield TestClient(app, raise_server_exceptions=False)


class TestSendCall:

    def test_sip_uri_uses_phone_number(self, client, mock_twilio):
        """The SIP URI user part is the E.164 From number (trunk matching)."""
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
        assert sip_to.startswith(
            "sip:+15550100006@"
        ), f"Expected E.164-based SIP URI, got: {sip_to}"
        assert sip_to.endswith(".sip.livekit.cloud")

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

    def test_sip_uri_uses_e164_format(self, client, mock_twilio):
        """The SIP URI preserves E.164 format with the + prefix."""
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
        assert sip_to == "sip:+447427857991@test.sip.livekit.cloud"
