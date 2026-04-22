"""Tests for the Teams meet PSTN dial-in bridge helpers + TwiML endpoint.

Covers:
  - ``_teams_meet_twiml`` TwiML shape (DTMF sequence, pause, callerId)
  - ``/phone/teams-meet-twiml`` endpoint param handling
  - ``initiate_teams_meet_bridge`` Twilio origination to LiveKit SIP +
    room-mapped dispatch rule
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from common.settings import SETTINGS
from communication.phone.views import (
    _teams_meet_twiml,
    initiate_teams_meet_bridge,
)

ENV = {
    "LIVEKIT_SIP_URI": "test.sip.livekit.cloud",
    "UNITY_COMMS_URL": "https://comms.example.com",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "test-token",
}


class TestTwimlShape:
    def test_contains_send_digits_with_pause_and_terminator(self):
        resp = _teams_meet_twiml(
            dial_in_number="+13235550123",
            conference_id="987654321",
            caller_id="+15550100006",
            ivr_pause_s=8,
        )
        xml = str(resp)
        assert '<Number sendDigits="wwwwwwww987654321#"' in xml
        assert "+13235550123</Number>" in xml
        assert 'callerId="+15550100006"' in xml

    def test_zero_pause_produces_bare_conference_id(self):
        resp = _teams_meet_twiml(
            dial_in_number="+13235550123",
            conference_id="111",
            caller_id="+15550100006",
            ivr_pause_s=0,
        )
        xml = str(resp)
        assert 'sendDigits="111#"' in xml

    def test_status_callback_wired_when_present(self):
        resp = _teams_meet_twiml(
            dial_in_number="+13235550123",
            conference_id="123",
            caller_id="+15550100006",
            ivr_pause_s=4,
            status_callback_url="https://adapters/twilio/call-status",
        )
        xml = str(resp)
        assert "statusCallback=" in xml
        assert "call-status" in xml


@pytest.fixture
def client():
    with (
        patch.dict(os.environ, ENV, clear=False),
        patch.object(SETTINGS, "comms_url", "https://comms.example.com"),
    ):
        from communication.main import app

        yield TestClient(app, raise_server_exceptions=False)


class TestTwimlEndpoint:
    def test_happy_path(self, client):
        resp = client.post(
            "/phone/teams-meet-twiml"
            "?dial_in=%2B13235550123"
            "&conf_id=987654321"
            "&caller_id=%2B12526595494"
            "&pause=4",
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/xml")
        xml = resp.text
        assert 'sendDigits="wwww987654321#"' in xml
        assert "+13235550123</Number>" in xml
        assert 'callerId="+15550100006"' in xml

    def test_missing_required_params_is_400(self, client):
        resp = client.post(
            "/phone/teams-meet-twiml?dial_in=%2B13235550123",
        )
        assert resp.status_code == 400


@pytest.fixture
def twilio_mock():
    client = MagicMock()
    call = MagicMock()
    call.sid = "CA_bridge_sid"
    client.calls.create.return_value = call
    return client


class TestInitiateBridge:
    @pytest.mark.asyncio
    async def test_originates_to_livekit_sip_uri(self, twilio_mock):
        with (
            patch.dict(os.environ, ENV, clear=False),
            patch.object(SETTINGS, "comms_url", "https://comms.example.com"),
            patch.object(SETTINGS, "teams_conferencing_ivr_pause_s", 8),
            patch(
                "communication.phone.views.ensure_phone_dispatch_rule",
                new=AsyncMock(),
            ) as ensure_mock,
            patch(
                "communication.phone.views.get_twilio_client",
                return_value=twilio_mock,
            ),
        ):
            sid = await initiate_teams_meet_bridge(
                assistant_twilio_did="+15550100006",
                dial_in_number="+13235550123",
                conference_id="987654321",
                room_name="unity_42_teams_meet",
                status_callback_url="https://adapters/twilio/call-status",
            )

        assert sid == "CA_bridge_sid"
        ensure_mock.assert_awaited_once_with(
            "+15550100006",
            "unity_42_teams_meet",
        )
        create_kwargs = twilio_mock.calls.create.call_args.kwargs
        assert create_kwargs["to"] == "sip:+15550100006@test.sip.livekit.cloud"
        assert create_kwargs["from_"] == "+15550100006"
        url = create_kwargs["url"]
        assert url.startswith(
            "https://comms.example.com/phone/teams-meet-twiml?",
        )
        # E.164 ``+`` prefixes MUST be percent-encoded so FastAPI doesn't
        # decode them as spaces on the way back in.
        assert "dial_in=%2B13235550123" in url
        assert "conf_id=987654321" in url
        assert "caller_id=%2B12526595494" in url
        assert "pause=8" in url
        assert "status_cb=" in url

    @pytest.mark.asyncio
    async def test_explicit_caller_id_and_pause_override(self, twilio_mock):
        with (
            patch.dict(os.environ, ENV, clear=False),
            patch.object(SETTINGS, "comms_url", "https://comms.example.com"),
            patch(
                "communication.phone.views.ensure_phone_dispatch_rule",
                new=AsyncMock(),
            ),
            patch(
                "communication.phone.views.get_twilio_client",
                return_value=twilio_mock,
            ),
        ):
            await initiate_teams_meet_bridge(
                assistant_twilio_did="+15550100006",
                dial_in_number="+13235550123",
                conference_id="111",
                room_name="unity_42_teams_meet",
                caller_id="+19999999999",
                ivr_pause_s=2,
            )

        url = twilio_mock.calls.create.call_args.kwargs["url"]
        assert "caller_id=%2B19999999999" in url
        assert "pause=2" in url
