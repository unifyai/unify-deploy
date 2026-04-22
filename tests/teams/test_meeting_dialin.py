"""Tests for ``communication/teams/meeting.py`` dial-in resolution.

Covers:
  - invite-body parsing across the common Outlook templates
  - Graph /me/onlineMeetings happy path + empty result
  - manual-override precedence over Graph + invite-body
  - fallthrough precedence (Graph > invite) when only Graph is reachable
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from communication.teams.meeting import (
    MeetingDialIn,
    fetch_dialin_from_graph,
    parse_dialin_from_invite,
    resolve_meeting_dialin,
)

# ---------------------------------------------------------------------------
# parse_dialin_from_invite
# ---------------------------------------------------------------------------


class TestParseInviteBody:
    def test_inline_phone_comma_comma_digits_form(self):
        body = (
            "Microsoft Teams meeting\n"
            "Join on your computer, mobile app or room device\n"
            "Or call in (audio only)\n"
            "+1 323-555-0123,,987654321#   United States, Los Angeles\n"
            "Phone Conference ID: 987 654 321#\n"
        )
        result = parse_dialin_from_invite(body)
        assert result is not None
        assert result.dial_in_number == "+13235550123"
        assert result.conference_id == "987654321"
        assert result.source == "invite_body"

    def test_html_invite_is_stripped(self):
        body = (
            "<html><body><p>Microsoft Teams meeting</p>"
            "<p>Or call in (audio only)</p>"
            "<p>+1 (646) 555-0199,,123456789#</p>"
            "<p>Phone Conference ID: 123 456 789 #</p>"
            "</body></html>"
        )
        result = parse_dialin_from_invite(body)
        assert result is not None
        assert result.dial_in_number == "+16465550199"
        assert result.conference_id == "123456789"

    def test_conference_id_only_form_with_audio_only_line(self):
        # Some localised invites drop the inline ``,,digits#`` marker.
        # The fallback path combines the ``audio only`` phone with the
        # ``Phone Conference ID`` line.
        body = (
            "Or call in (audio only)\n"
            "+44 20 7946 0987 United Kingdom, London\n"
            "Phone Conference ID: 555 000 111\n"
        )
        result = parse_dialin_from_invite(body)
        assert result is not None
        assert result.dial_in_number == "+442079460987"
        assert result.conference_id == "555000111"

    def test_missing_conference_id_returns_none(self):
        body = "Or call in (audio only) +1 415-555-0000 San Francisco\n"
        assert parse_dialin_from_invite(body) is None

    def test_empty_body_returns_none(self):
        assert parse_dialin_from_invite("") is None
        assert parse_dialin_from_invite(None) is None


# ---------------------------------------------------------------------------
# fetch_dialin_from_graph
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    """Minimal async-context-manager stand-in for ``httpx.AsyncClient``."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_url: str | None = None
        self.last_params: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, headers=None):
        self.last_url = url
        self.last_params = params
        return self._response


class TestFetchDialinFromGraph:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        body = {
            "value": [
                {
                    "audioConferencing": {
                        "conferenceId": "987654321",
                        "tollNumber": "+1 323-555-0123",
                        "tollFreeNumber": "+1 800 555 0199",
                    },
                },
            ],
        }
        fake = _FakeClient(_FakeResponse(200, body))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            result = await fetch_dialin_from_graph(
                "tok",
                "https://teams.microsoft.com/l/meetup-join/abc",
            )
        assert result is not None
        assert result.dial_in_number == "+13235550123"
        assert result.conference_id == "987654321"
        assert result.source == "graph"
        assert result.toll_free_number == "+18005550199"
        assert fake.last_params == {
            "$filter": "JoinWebUrl eq 'https://teams.microsoft.com/l/meetup-join/abc'",
        }

    @pytest.mark.asyncio
    async def test_extracts_organizer_and_subject(self):
        body = {
            "value": [
                {
                    "subject": "Quarterly review",
                    "audioConferencing": {
                        "conferenceId": "987654321",
                        "tollNumber": "+1 323-555-0123",
                    },
                    "participants": {
                        "organizer": {
                            "upn": "alice@contoso.com",
                            "identity": {
                                "user": {
                                    "id": "u-123",
                                    "displayName": "Alice Smith",
                                },
                            },
                        },
                    },
                },
            ],
        }
        fake = _FakeClient(_FakeResponse(200, body))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            result = await fetch_dialin_from_graph(
                "tok",
                "https://teams.microsoft.com/l/meetup-join/abc",
            )
        assert result is not None
        assert result.organizer_email == "alice@contoso.com"
        assert result.organizer_name == "Alice Smith"
        assert result.meeting_subject == "Quarterly review"

    @pytest.mark.asyncio
    async def test_missing_organizer_block_leaves_fields_none(self):
        body = {
            "value": [
                {
                    "audioConferencing": {
                        "conferenceId": "987654321",
                        "tollNumber": "+1 323-555-0123",
                    },
                },
            ],
        }
        fake = _FakeClient(_FakeResponse(200, body))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            result = await fetch_dialin_from_graph("tok", "https://x")
        assert result is not None
        assert result.organizer_email is None
        assert result.organizer_name is None
        assert result.meeting_subject is None

    @pytest.mark.asyncio
    async def test_empty_value_returns_none(self):
        fake = _FakeClient(_FakeResponse(200, {"value": []}))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            assert await fetch_dialin_from_graph("tok", "https://example.com") is None

    @pytest.mark.asyncio
    async def test_no_audio_conferencing_returns_none(self):
        # Some meetings exist but have no Audio Conferencing SKU attached.
        fake = _FakeClient(_FakeResponse(200, {"value": [{"id": "abc"}]}))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            assert await fetch_dialin_from_graph("tok", "https://example.com") is None

    @pytest.mark.asyncio
    async def test_403_raises_permission_error(self):
        fake = _FakeClient(_FakeResponse(403, {}, text="Forbidden"))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            with pytest.raises(PermissionError):
                await fetch_dialin_from_graph("tok", "https://example.com")

    @pytest.mark.asyncio
    async def test_other_error_returns_none(self):
        fake = _FakeClient(_FakeResponse(500, {}, text="boom"))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            assert await fetch_dialin_from_graph("tok", "https://example.com") is None

    @pytest.mark.asyncio
    async def test_missing_token_or_url_returns_none(self):
        assert await fetch_dialin_from_graph("", "https://x") is None
        assert await fetch_dialin_from_graph("tok", "") is None


# ---------------------------------------------------------------------------
# resolve_meeting_dialin (precedence)
# ---------------------------------------------------------------------------


class TestResolvePrecedence:
    @pytest.mark.asyncio
    async def test_manual_overrides_everything(self):
        with patch(
            "communication.teams.meeting.fetch_dialin_from_graph",
            new=AsyncMock(
                return_value=MeetingDialIn(
                    dial_in_number="+19000000000",
                    conference_id="111",
                    source="graph",
                ),
            ),
        ) as graph_mock:
            result = await resolve_meeting_dialin(
                access_token="tok",
                join_web_url="https://x",
                invite_body="some body",
                manual_dial_in_number="+1 323-555-0199",
                manual_conference_id="222 333",
            )
        assert result is not None
        assert result.source == "manual"
        assert result.dial_in_number == "+13235550199"
        assert result.conference_id == "222333"
        graph_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_graph_wins_over_invite_body(self):
        invite = (
            "Or call in (audio only)\n"
            "+1 415-555-0000,,999888777#\n"
            "Phone Conference ID: 999 888 777\n"
        )
        with patch(
            "communication.teams.meeting.fetch_dialin_from_graph",
            new=AsyncMock(
                return_value=MeetingDialIn(
                    dial_in_number="+13235550123",
                    conference_id="123456",
                    source="graph",
                ),
            ),
        ):
            result = await resolve_meeting_dialin(
                access_token="tok",
                join_web_url="https://x",
                invite_body=invite,
            )
        assert result is not None
        assert result.source == "graph"
        assert result.conference_id == "123456"

    @pytest.mark.asyncio
    async def test_falls_back_to_invite_when_graph_empty(self):
        invite = (
            "Or call in (audio only)\n"
            "+1 415-555-0000,,999888777#\n"
            "Phone Conference ID: 999 888 777\n"
        )
        with patch(
            "communication.teams.meeting.fetch_dialin_from_graph",
            new=AsyncMock(return_value=None),
        ):
            result = await resolve_meeting_dialin(
                access_token="tok",
                join_web_url="https://x",
                invite_body=invite,
            )
        assert result is not None
        assert result.source == "invite_body"
        assert result.dial_in_number == "+14155550000"

    @pytest.mark.asyncio
    async def test_permission_error_falls_through_to_invite(self):
        invite = (
            "Or call in (audio only)\n"
            "+1 415-555-0000,,999888777#\n"
            "Phone Conference ID: 999 888 777\n"
        )
        with patch(
            "communication.teams.meeting.fetch_dialin_from_graph",
            new=AsyncMock(side_effect=PermissionError("forbidden")),
        ):
            result = await resolve_meeting_dialin(
                access_token="tok",
                join_web_url="https://x",
                invite_body=invite,
            )
        assert result is not None
        assert result.source == "invite_body"

    @pytest.mark.asyncio
    async def test_nothing_available_returns_none(self):
        assert (
            await resolve_meeting_dialin(
                access_token=None,
                join_web_url=None,
                invite_body=None,
            )
            is None
        )
