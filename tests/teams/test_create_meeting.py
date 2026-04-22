"""Tests for ``POST /teams/create_meeting`` and the Graph helpers it calls.

Covers:
  - instant ``POST /me/onlineMeetings`` happy path
  - scheduled ``POST /me/events`` happy path with attendees + body
  - validation errors (missing email, bad mode, missing scheduled fields)
  - 403 when the assistant has no MICROSOFT_ACCESS_TOKEN
  - PermissionError surfaces as 403, RuntimeError as 502
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from common.settings import SETTINGS
from communication.teams.create_meeting import (
    CreatedMeeting,
    create_instant_onlinemeeting,
    create_scheduled_meeting_event,
)

ENV = {
    "UNITY_COMMS_URL": "https://comms.example.com",
    "UNITY_ADAPTERS_URL": "https://adapters.example.com",
    "GCP_SA_KEY": "{}",
}


# ---------------------------------------------------------------------------
# Direct helper tests (httpx-mocked)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_url: str | None = None
        self.last_json: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        self.last_url = url
        self.last_json = json
        return self._response


class TestCreateInstantOnlineMeeting:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        body = {
            "id": "meeting-id-1",
            "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/xyz",
            "subject": "Test",
            "startDateTime": "2026-05-01T15:00:00Z",
            "endDateTime": "2026-05-01T16:00:00Z",
        }
        fake = _FakeClient(_FakeResponse(201, body))
        with patch(
            "communication.teams.create_meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            result = await create_instant_onlinemeeting(
                "tok",
                subject="Test",
                start_datetime="2026-05-01T15:00:00Z",
                end_datetime="2026-05-01T16:00:00Z",
            )
        assert isinstance(result, CreatedMeeting)
        assert result.join_web_url.startswith("https://teams.microsoft.com")
        assert result.meeting_id == "meeting-id-1"
        assert result.subject == "Test"
        assert fake.last_url.endswith("/me/onlineMeetings")
        assert fake.last_json == {
            "subject": "Test",
            "startDateTime": "2026-05-01T15:00:00Z",
            "endDateTime": "2026-05-01T16:00:00Z",
        }

    @pytest.mark.asyncio
    async def test_omits_optional_fields(self):
        body = {
            "id": "m",
            "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/y",
        }
        fake = _FakeClient(_FakeResponse(201, body))
        with patch(
            "communication.teams.create_meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            await create_instant_onlinemeeting("tok")
        assert fake.last_json == {}

    @pytest.mark.asyncio
    async def test_403_raises_permission_error(self):
        fake = _FakeClient(_FakeResponse(403, {}, text="Forbidden"))
        with patch(
            "communication.teams.create_meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            with pytest.raises(PermissionError):
                await create_instant_onlinemeeting("tok")

    @pytest.mark.asyncio
    async def test_500_raises_runtime_error(self):
        fake = _FakeClient(_FakeResponse(500, {}, text="boom"))
        with patch(
            "communication.teams.create_meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            with pytest.raises(RuntimeError):
                await create_instant_onlinemeeting("tok")

    @pytest.mark.asyncio
    async def test_missing_join_url_raises(self):
        fake = _FakeClient(_FakeResponse(201, {"id": "m"}))
        with patch(
            "communication.teams.create_meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            with pytest.raises(RuntimeError):
                await create_instant_onlinemeeting("tok")

    @pytest.mark.asyncio
    async def test_empty_token_raises(self):
        with pytest.raises(ValueError):
            await create_instant_onlinemeeting("")


class TestCreateScheduledMeetingEvent:
    @pytest.mark.asyncio
    async def test_happy_path_with_attendees_and_body(self):
        body = {
            "id": "event-id-1",
            "subject": "Quarterly review",
            "start": {"dateTime": "2026-05-01T15:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-05-01T16:00:00", "timeZone": "UTC"},
            "webLink": "https://outlook.office.com/owa/?itemid=xyz",
            "onlineMeeting": {
                "joinUrl": "https://teams.microsoft.com/l/meetup-join/abc",
            },
        }
        fake = _FakeClient(_FakeResponse(201, body))
        with patch(
            "communication.teams.create_meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            result = await create_scheduled_meeting_event(
                "tok",
                subject="Quarterly review",
                start_datetime="2026-05-01T15:00:00",
                end_datetime="2026-05-01T16:00:00",
                attendees=["alice@example.com", "bob@example.com"],
                body_html="<p>Agenda</p>",
                location="Online",
            )
        assert result.event_id == "event-id-1"
        assert result.web_link == "https://outlook.office.com/owa/?itemid=xyz"
        assert result.join_web_url.endswith("/abc")
        sent = fake.last_json
        assert sent["isOnlineMeeting"] is True
        assert sent["onlineMeetingProvider"] == "teamsForBusiness"
        assert sent["start"] == {
            "dateTime": "2026-05-01T15:00:00",
            "timeZone": "UTC",
        }
        assert sent["body"] == {"contentType": "HTML", "content": "<p>Agenda</p>"}
        assert sent["location"] == {"displayName": "Online"}
        assert {a["emailAddress"]["address"] for a in sent["attendees"]} == {
            "alice@example.com",
            "bob@example.com",
        }
        assert all(a["type"] == "required" for a in sent["attendees"])

    @pytest.mark.asyncio
    async def test_missing_join_url_raises(self):
        body = {
            "id": "ev",
            "start": {"dateTime": "2026-05-01T15:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-05-01T16:00:00", "timeZone": "UTC"},
        }
        fake = _FakeClient(_FakeResponse(201, body))
        with patch(
            "communication.teams.create_meeting.httpx.AsyncClient",
            return_value=fake,
        ):
            with pytest.raises(RuntimeError):
                await create_scheduled_meeting_event(
                    "tok",
                    subject="x",
                    start_datetime="2026-05-01T15:00:00",
                    end_datetime="2026-05-01T16:00:00",
                )

    @pytest.mark.asyncio
    async def test_missing_required_args_raises(self):
        with pytest.raises(ValueError):
            await create_scheduled_meeting_event(
                "tok",
                subject="",
                start_datetime="2026-05-01T15:00:00",
                end_datetime="2026-05-01T16:00:00",
            )


# ---------------------------------------------------------------------------
# Endpoint integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def assistant_record():
    return {
        "agent_id": "42",
        "assistant_id": "42",
        "user_id": "7",
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
            "communication.teams.views.publish_assistant_event",
            new=lambda *a, **k: None,
        ),
    ):
        from communication.main import app

        tc = TestClient(app, raise_server_exceptions=False)
        tc.headers["Authorization"] = "Bearer test-admin-key"
        yield tc


class TestCreateMeetingEndpoint:
    def test_instant_happy_path(self, client):
        with patch(
            "communication.teams.create_meeting.create_instant_onlinemeeting",
            new=AsyncMock(
                return_value=CreatedMeeting(
                    join_web_url="https://teams.microsoft.com/l/meetup-join/abc",
                    meeting_id="m-1",
                    subject="Sync",
                ),
            ),
        ) as mock_create:
            resp = client.post(
                "/teams/create_meeting",
                json={
                    "assistant_email": "assistant@contoso.com",
                    "mode": "instant",
                    "subject": "Sync",
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["join_web_url"].endswith("/abc")
        assert body["meeting_id"] == "m-1"
        assert body["event_id"] is None
        mock_create.assert_awaited_once()
        kwargs = mock_create.await_args.kwargs
        assert kwargs == {
            "subject": "Sync",
            "start_datetime": None,
            "end_datetime": None,
        }

    def test_default_mode_is_instant(self, client):
        with patch(
            "communication.teams.create_meeting.create_instant_onlinemeeting",
            new=AsyncMock(
                return_value=CreatedMeeting(
                    join_web_url="https://x",
                    meeting_id="m",
                ),
            ),
        ):
            resp = client.post(
                "/teams/create_meeting",
                json={"assistant_email": "assistant@contoso.com"},
            )
        assert resp.status_code == 200, resp.text

    def test_scheduled_happy_path(self, client):
        with patch(
            "communication.teams.create_meeting.create_scheduled_meeting_event",
            new=AsyncMock(
                return_value=CreatedMeeting(
                    join_web_url="https://teams.microsoft.com/l/meetup-join/xyz",
                    event_id="e-1",
                    subject="Quarterly review",
                    start_datetime="2026-05-01T15:00:00",
                    end_datetime="2026-05-01T16:00:00",
                    web_link="https://outlook.office.com/owa/?itemid=xyz",
                ),
            ),
        ) as mock_create:
            resp = client.post(
                "/teams/create_meeting",
                json={
                    "assistant_email": "assistant@contoso.com",
                    "mode": "scheduled",
                    "subject": "Quarterly review",
                    "start": "2026-05-01T15:00:00",
                    "end": "2026-05-01T16:00:00",
                    "attendees": ["alice@example.com"],
                    "body": "<p>Agenda</p>",
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["event_id"] == "e-1"
        assert body["web_link"].startswith("https://outlook.office.com")
        kwargs = mock_create.await_args.kwargs
        assert kwargs["subject"] == "Quarterly review"
        assert kwargs["attendees"] == ["alice@example.com"]
        assert kwargs["body_html"] == "<p>Agenda</p>"
        assert kwargs["timezone"] == "UTC"

    def test_missing_assistant_email_returns_400(self, client):
        resp = client.post("/teams/create_meeting", json={"mode": "instant"})
        assert resp.status_code == 400

    def test_invalid_mode_returns_400(self, client):
        resp = client.post(
            "/teams/create_meeting",
            json={"assistant_email": "a@b.com", "mode": "bogus"},
        )
        assert resp.status_code == 400

    def test_scheduled_missing_fields_returns_400(self, client):
        resp = client.post(
            "/teams/create_meeting",
            json={
                "assistant_email": "assistant@contoso.com",
                "mode": "scheduled",
                "subject": "x",
            },
        )
        assert resp.status_code == 400

    def test_no_token_returns_409(self, client):
        with patch(
            "communication.teams.views._lookup_assistant",
            new=AsyncMock(return_value={"assistant_id": "42", "secrets": {}}),
        ):
            resp = client.post(
                "/teams/create_meeting",
                json={"assistant_email": "assistant@contoso.com"},
            )
        assert resp.status_code == 409

    def test_permission_error_returns_403(self, client):
        with patch(
            "communication.teams.create_meeting.create_instant_onlinemeeting",
            new=AsyncMock(side_effect=PermissionError("forbidden")),
        ):
            resp = client.post(
                "/teams/create_meeting",
                json={"assistant_email": "a@b.com"},
            )
        assert resp.status_code == 403

    def test_runtime_error_returns_502(self, client):
        with patch(
            "communication.teams.create_meeting.create_instant_onlinemeeting",
            new=AsyncMock(side_effect=RuntimeError("graph down")),
        ):
            resp = client.post(
                "/teams/create_meeting",
                json={"assistant_email": "a@b.com"},
            )
        assert resp.status_code == 502
