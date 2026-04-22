"""Tests for ``POST /teams/join_meeting`` and ``POST /teams/leave_meeting``.

Exercises end-to-end dispatch: assistant lookup → dial-in resolution →
LiveKit room + agent dispatch → Twilio outbound bridge, with all three
external integrations mocked.  Focus is on request/response contract and
that the Twilio bridge call receives the correct dial-in number + room.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from common.settings import SETTINGS
from communication.teams.meeting import MeetingDialIn

ENV = {
    "LIVEKIT_SIP_URI": "test.sip.livekit.cloud",
    "UNITY_COMMS_URL": "https://comms.example.com",
    "UNITY_ADAPTERS_URL": "https://adapters.example.com",
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "test-token",
    "GCP_SA_KEY": "{}",
}


@pytest.fixture
def assistant_record():
    return {
        "agent_id": "42",
        "assistant_id": "42",
        "user_id": "7",
        "phone": "+15550100006",
        "secrets": {"MICROSOFT_ACCESS_TOKEN": "tok"},
    }


@pytest.fixture
def client(assistant_record):
    with (
        patch.dict(os.environ, ENV, clear=False),
        patch.object(SETTINGS, "orchestra_admin_key", "test-admin-key"),
        patch.object(SETTINGS, "comms_url", "https://comms.example.com"),
        patch.object(SETTINGS, "adapters_url", "https://adapters.example.com"),
        patch(
            "communication.teams.views._lookup_assistant",
            new=AsyncMock(return_value=assistant_record),
        ),
        patch(
            "communication.teams.views.create_room_and_dispatch_agent",
            new=AsyncMock(return_value=MagicMock(id="dispatch-123")),
        ) as mock_dispatch,
        patch(
            "communication.teams.views.initiate_teams_meet_bridge",
            new=AsyncMock(return_value="CA_call_sid"),
        ) as mock_bridge,
        patch(
            "communication.teams.views._publish_teams_meet_received",
            new=AsyncMock(),
        ) as mock_publish,
    ):
        from communication.main import app

        tc = TestClient(app, raise_server_exceptions=False)
        tc.headers["Authorization"] = "Bearer test-admin-key"
        tc.mock_dispatch = mock_dispatch
        tc.mock_bridge = mock_bridge
        tc.mock_publish = mock_publish
        yield tc


class TestJoinMeetingViaGraph:
    def test_graph_resolution_happy_path(self, client):
        resolved = MeetingDialIn(
            dial_in_number="+13235550123",
            conference_id="987654321",
            source="graph",
            organizer_email="organizer@contoso.com",
            organizer_name="Alice Smith",
        )
        with patch(
            "communication.teams.views.resolve_meeting_dialin",
            new=AsyncMock(return_value=resolved),
        ):
            resp = client.post(
                "/teams/join_meeting",
                json={
                    "assistant_email": "assistant@contoso.com",
                    "join_web_url": "https://teams.microsoft.com/l/meetup-join/abc",
                    "subject": "1:1 with Alice",
                },
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["call_sid"] == "CA_call_sid"
        assert body["room_name"] == "unity_42_teams_meet"
        assert body["conference_name"].startswith("Unity_TeamsMeet_42_")
        assert body["dial_in_source"] == "graph"
        assert body["dial_in_number"] == "+13235550123"
        assert body["conference_id"] == "987654321"

        bridge_kwargs = client.mock_bridge.await_args.kwargs
        assert bridge_kwargs["assistant_twilio_did"] == "+15550100006"
        assert bridge_kwargs["dial_in_number"] == "+13235550123"
        assert bridge_kwargs["conference_id"] == "987654321"
        assert bridge_kwargs["room_name"] == "unity_42_teams_meet"
        assert bridge_kwargs["caller_id"] == "+15550100006"
        status_cb = bridge_kwargs["status_callback_url"]
        # Dedicated Teams-meet endpoint in adapters — no ``leg=`` tag,
        # the path itself is the discriminator so the generic
        # ``/twilio/call-status`` handler stays focused on phone calls.
        assert "/twilio/teams-meet-call-status" in status_cb
        assert "leg=teams_meet" not in status_cb
        assert "livekit_room=unity_42_teams_meet" in status_cb
        assert "assistant_id=42" in status_cb
        assert "conference_name=Unity_TeamsMeet_42_" in status_cb

        dispatch_kwargs = client.mock_dispatch.await_args
        assert dispatch_kwargs.args[0] == "unity_42_teams_meet"
        assert dispatch_kwargs.kwargs["metadata"]["channel"] == "teams_meet"
        assert (
            dispatch_kwargs.kwargs["metadata"]["assistant_email"]
            == "assistant@contoso.com"
        )
        assert (
            dispatch_kwargs.kwargs["metadata"]["conference_name"]
            == body["conference_name"]
        )
        assert dispatch_kwargs.kwargs["record"] is False
        assert dispatch_kwargs.kwargs["assistant_id"] == "42"
        assert dispatch_kwargs.kwargs["user_id"] == "7"

        # Pub/Sub contract: ``thread: "teams_meet"`` published with the
        # same room/conference/dial-in the LiveKit agent and Twilio
        # leg are using.
        publish_kwargs = client.mock_publish.await_args.kwargs
        assert publish_kwargs["assistant_id"] == "42"
        assert publish_kwargs["assistant_email"] == "assistant@contoso.com"
        assert publish_kwargs["room_name"] == "unity_42_teams_meet"
        assert publish_kwargs["conference_name"] == body["conference_name"]
        assert publish_kwargs["call_sid"] == "CA_call_sid"
        assert publish_kwargs["dialin"] is resolved
        assert publish_kwargs["organizer_email"] == "organizer@contoso.com"
        assert publish_kwargs["organizer_name"] == "Alice Smith"


class TestJoinMeetingFromInviteBody:
    def test_invite_body_fallback(self, client):
        resolved = MeetingDialIn(
            dial_in_number="+14155550000",
            conference_id="999888777",
            source="invite_body",
        )
        with patch(
            "communication.teams.views.resolve_meeting_dialin",
            new=AsyncMock(return_value=resolved),
        ):
            resp = client.post(
                "/teams/join_meeting",
                json={
                    "assistant_email": "assistant@contoso.com",
                    "invite_body": (
                        "Or call in (audio only)\n"
                        "+1 415-555-0000,,999888777#\n"
                        "Phone Conference ID: 999 888 777\n"
                    ),
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["dial_in_source"] == "invite_body"


class TestJoinMeetingManualOverride:
    def test_manual_override(self, client):
        resolved = MeetingDialIn(
            dial_in_number="+13235550199",
            conference_id="222333",
            source="manual",
        )
        with patch(
            "communication.teams.views.resolve_meeting_dialin",
            new=AsyncMock(return_value=resolved),
        ):
            resp = client.post(
                "/teams/join_meeting",
                json={
                    "assistant_email": "assistant@contoso.com",
                    "dial_in_number": "+1 323-555-0199",
                    "conference_id": "222 333",
                },
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["dial_in_source"] == "manual"


class TestJoinMeetingErrors:
    def test_missing_everything_is_400(self, client):
        resp = client.post(
            "/teams/join_meeting",
            json={"assistant_email": "assistant@contoso.com"},
        )
        assert resp.status_code == 400
        assert "join_web_url" in resp.text

    def test_missing_assistant_email_is_400(self, client):
        resp = client.post(
            "/teams/join_meeting",
            json={
                "join_web_url": "https://teams.microsoft.com/l/meetup-join/abc",
            },
        )
        assert resp.status_code == 400

    def test_unresolvable_dialin_is_422(self, client):
        with patch(
            "communication.teams.views.resolve_meeting_dialin",
            new=AsyncMock(return_value=None),
        ):
            resp = client.post(
                "/teams/join_meeting",
                json={
                    "assistant_email": "assistant@contoso.com",
                    "join_web_url": "https://teams.microsoft.com/l/meetup-join/abc",
                },
            )
        assert resp.status_code == 422
        assert "dial-in" in resp.text.lower()

    def test_assistant_without_phone_is_409(self, client):
        resolved = MeetingDialIn(
            dial_in_number="+13235550123",
            conference_id="987654321",
            source="graph",
        )
        no_phone = {
            "agent_id": "42",
            "assistant_id": "42",
            "user_id": "7",
            "phone": "",
            "secrets": {"MICROSOFT_ACCESS_TOKEN": "tok"},
        }
        with (
            patch(
                "communication.teams.views._lookup_assistant",
                new=AsyncMock(return_value=no_phone),
            ),
            patch(
                "communication.teams.views.resolve_meeting_dialin",
                new=AsyncMock(return_value=resolved),
            ),
        ):
            resp = client.post(
                "/teams/join_meeting",
                json={
                    "assistant_email": "assistant@contoso.com",
                    "join_web_url": "https://teams.microsoft.com/l/meetup-join/abc",
                },
            )
        assert resp.status_code == 409
        assert "phone number" in resp.text.lower()


class TestLeaveMeeting:
    def test_hangs_up_by_call_sid(self, client):
        mock_twilio = MagicMock()
        with patch(
            "communication.teams.views.get_twilio_client",
            return_value=mock_twilio,
        ):
            resp = client.post(
                "/teams/leave_meeting",
                json={"call_sid": "CA_xyz"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["call_sid"] == "CA_xyz"
        mock_twilio.calls.assert_called_once_with("CA_xyz")
        mock_twilio.calls.return_value.update.assert_called_once_with(
            status="completed",
        )

    def test_hangs_up_by_conference_name(self, client):
        mock_twilio = MagicMock()
        with (
            patch(
                "communication.teams.views.get_twilio_client",
                return_value=mock_twilio,
            ),
            patch(
                "communication.teams.views._find_teams_meet_call_sid",
                return_value="CA_from_conf",
            ) as mock_find,
        ):
            resp = client.post(
                "/teams/leave_meeting",
                json={"conference_name": "Unity_TeamsMeet_42_20260101_120000"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["call_sid"] == "CA_from_conf"
        assert body["conference_name"] == "Unity_TeamsMeet_42_20260101_120000"
        mock_find.assert_called_once_with(
            mock_twilio, "Unity_TeamsMeet_42_20260101_120000"
        )
        mock_twilio.calls.assert_called_once_with("CA_from_conf")

    def test_conference_name_no_active_call_is_success(self, client):
        with (
            patch(
                "communication.teams.views.get_twilio_client",
                return_value=MagicMock(),
            ),
            patch(
                "communication.teams.views._find_teams_meet_call_sid",
                return_value=None,
            ),
        ):
            resp = client.post(
                "/teams/leave_meeting",
                json={"conference_name": "Unity_TeamsMeet_42_ancient"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["already_ended"] is True

    def test_missing_everything_is_400(self, client):
        resp = client.post("/teams/leave_meeting", json={})
        assert resp.status_code == 400

    def test_404_from_twilio_is_treated_as_success(self, client):
        from twilio.base.exceptions import TwilioRestException

        mock_twilio = MagicMock()
        mock_twilio.calls.return_value.update.side_effect = TwilioRestException(
            status=404,
            uri="/x",
            msg="Not Found",
        )
        with patch(
            "communication.teams.views.get_twilio_client",
            return_value=mock_twilio,
        ):
            resp = client.post(
                "/teams/leave_meeting",
                json={"call_sid": "CA_already_gone"},
            )
        assert resp.status_code == 200
