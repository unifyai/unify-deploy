"""Tests for ``communication/teams/meeting.py`` Graph metadata lookup.

Covers ``fetch_onlinemeeting_by_joinurl``: happy path, organizer/subject
extraction, empty results, missing inputs, and error surfaces.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from communication.teams.meeting import (
    OnlineMeetingInfo,
    fetch_onlinemeeting_by_joinurl,
)


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


class TestFetchOnlineMeetingByJoinUrl:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        body = {
            "value": [
                {
                    "id": "meeting-id-abc",
                    "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/abc",
                    "subject": "Quarterly review",
                    "startDateTime": "2026-05-01T15:00:00Z",
                    "endDateTime": "2026-05-01T16:00:00Z",
                },
            ],
        }
        fake = _FakeClient(_FakeResponse(200, body))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            result = await fetch_onlinemeeting_by_joinurl(
                "tok",
                "https://teams.microsoft.com/l/meetup-join/abc",
            )
        assert isinstance(result, OnlineMeetingInfo)
        assert result.meeting_id == "meeting-id-abc"
        assert result.subject == "Quarterly review"
        assert result.start_datetime == "2026-05-01T15:00:00Z"
        assert result.end_datetime == "2026-05-01T16:00:00Z"
        assert fake.last_params == {
            "$filter": "JoinWebUrl eq 'https://teams.microsoft.com/l/meetup-join/abc'",
        }

    @pytest.mark.asyncio
    async def test_extracts_organizer(self):
        body = {
            "value": [
                {
                    "id": "m-1",
                    "joinWebUrl": "https://x",
                    "subject": "Sync",
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
            result = await fetch_onlinemeeting_by_joinurl(
                "tok",
                "https://x",
            )
        assert result is not None
        assert result.organizer_email == "alice@contoso.com"
        assert result.organizer_name == "Alice Smith"

    @pytest.mark.asyncio
    async def test_missing_organizer_block_leaves_fields_none(self):
        body = {"value": [{"id": "m", "joinWebUrl": "https://x"}]}
        fake = _FakeClient(_FakeResponse(200, body))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            result = await fetch_onlinemeeting_by_joinurl("tok", "https://x")
        assert result is not None
        assert result.organizer_email is None
        assert result.organizer_name is None
        assert result.subject is None

    @pytest.mark.asyncio
    async def test_empty_value_returns_none(self):
        fake = _FakeClient(_FakeResponse(200, {"value": []}))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            assert (
                await fetch_onlinemeeting_by_joinurl("tok", "https://example.com")
                is None
            )

    @pytest.mark.asyncio
    async def test_403_raises_permission_error(self):
        fake = _FakeClient(_FakeResponse(403, {}, text="Forbidden"))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            with pytest.raises(PermissionError):
                await fetch_onlinemeeting_by_joinurl("tok", "https://example.com")

    @pytest.mark.asyncio
    async def test_other_error_returns_none(self):
        fake = _FakeClient(_FakeResponse(500, {}, text="boom"))
        with patch(
            "communication.teams.meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            assert (
                await fetch_onlinemeeting_by_joinurl("tok", "https://example.com")
                is None
            )

    @pytest.mark.asyncio
    async def test_missing_token_or_url_returns_none(self):
        assert await fetch_onlinemeeting_by_joinurl("", "https://x") is None
        assert await fetch_onlinemeeting_by_joinurl("tok", "") is None
