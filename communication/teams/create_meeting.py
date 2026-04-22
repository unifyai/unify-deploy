"""Create Microsoft Teams online meetings via the Graph API.

Two creation modes are supported:

1. **Instant meeting** — :func:`create_instant_onlinemeeting`
   ``POST /me/onlineMeetings`` returns a meeting + ``joinWebUrl`` without
   placing anything on the organizer's calendar.  Suitable for ad-hoc
   browser-join flows where the assistant just needs a URL to drive into.

2. **Scheduled calendar event** — :func:`create_scheduled_meeting_event`
   ``POST /me/events`` with ``isOnlineMeeting=true`` creates a real
   calendar entry (with attendees, subject, body, start/end) and lets
   Graph attach an embedded Teams meeting whose ``joinUrl`` is exposed
   via ``onlineMeeting.joinUrl``.

Both helpers are thin wrappers around raw httpx requests — the msgraph
Python SDK requires constructing several model objects per call which
isn't worth the indirection here.

The browser-automation join flow only needs ``join_web_url``; everything
else (subject, organizer, attendees) is metadata returned to the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

import httpx

logger = logging.getLogger(__name__)


_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


@dataclass(frozen=True)
class CreatedMeeting:
    """What both creation flows return.

    ``join_web_url`` is the only field guaranteed to be populated; the
    rest mirror what Graph echoes back so callers can persist a richer
    record without a follow-up lookup.
    """

    join_web_url: str
    meeting_id: Optional[str] = None
    event_id: Optional[str] = None
    subject: Optional[str] = None
    start_datetime: Optional[str] = None
    end_datetime: Optional[str] = None
    web_link: Optional[str] = None


def _raise_for_graph_error(resp: httpx.Response, op: str) -> None:
    """Translate Graph error shapes into clearer Python exceptions."""
    if resp.status_code in (401, 403):
        raise PermissionError(
            f"Graph {op} rejected token: {resp.status_code} {resp.text}",
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Graph {op} failed: {resp.status_code} {resp.text}",
        )


async def create_instant_onlinemeeting(
    access_token: str,
    *,
    subject: Optional[str] = None,
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    timeout_s: float = 20.0,
) -> CreatedMeeting:
    """Create a Teams online meeting without a calendar event.

    All arguments except ``access_token`` are optional.  When ``start``/
    ``end`` are omitted Graph creates a "reusable" meeting valid for ~60
    days; when supplied they bound the meeting window.

    Times must be ISO-8601 (e.g. ``"2026-05-01T15:00:00Z"``).  Graph
    accepts ``Z``-suffixed UTC or offset-bearing strings.
    """
    if not access_token:
        raise ValueError("access_token is required")

    body: dict = {}
    if subject:
        body["subject"] = subject
    if start_datetime:
        body["startDateTime"] = start_datetime
    if end_datetime:
        body["endDateTime"] = end_datetime

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(
            f"{_GRAPH_BASE}/me/onlineMeetings",
            json=body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )

    _raise_for_graph_error(resp, "POST /me/onlineMeetings")

    data = resp.json() or {}
    join_url = data.get("joinWebUrl") or ""
    if not join_url:
        raise RuntimeError(
            f"Graph returned an onlineMeeting without joinWebUrl: {data!r}",
        )

    return CreatedMeeting(
        join_web_url=join_url,
        meeting_id=data.get("id") or None,
        subject=(data.get("subject") or "").strip() or None,
        start_datetime=data.get("startDateTime") or None,
        end_datetime=data.get("endDateTime") or None,
    )


async def create_scheduled_meeting_event(
    access_token: str,
    *,
    subject: str,
    start_datetime: str,
    end_datetime: str,
    timezone: str = "UTC",
    attendees: Optional[Iterable[str]] = None,
    body_html: Optional[str] = None,
    location: Optional[str] = None,
    timeout_s: float = 20.0,
) -> CreatedMeeting:
    """Create a calendar event with an attached Teams meeting.

    Requires ``Calendars.ReadWrite`` (and ``OnlineMeetings.ReadWrite``
    when the tenant policy gates online-meeting attachment).

    ``body_html`` is sent verbatim with ``contentType: "HTML"``; pass
    plain text and Graph will render it literally.

    ``attendees`` are added as required participants.  Optional or
    resource attendees are out of scope here.
    """
    if not access_token:
        raise ValueError("access_token is required")
    if not (subject and start_datetime and end_datetime):
        raise ValueError("subject, start_datetime, end_datetime are required")

    payload: dict = {
        "subject": subject,
        "start": {"dateTime": start_datetime, "timeZone": timezone},
        "end": {"dateTime": end_datetime, "timeZone": timezone},
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
    }
    if body_html is not None:
        payload["body"] = {"contentType": "HTML", "content": body_html}
    if location:
        payload["location"] = {"displayName": location}
    if attendees:
        payload["attendees"] = [
            {
                "emailAddress": {"address": email},
                "type": "required",
            }
            for email in attendees
            if email
        ]

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(
            f"{_GRAPH_BASE}/me/events",
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )

    _raise_for_graph_error(resp, "POST /me/events")

    data = resp.json() or {}
    online = data.get("onlineMeeting") or {}
    join_url = online.get("joinUrl") or ""
    if not join_url:
        raise RuntimeError(
            f"Graph created event without onlineMeeting.joinUrl: {data!r}",
        )

    start = (data.get("start") or {}).get("dateTime") or None
    end = (data.get("end") or {}).get("dateTime") or None

    return CreatedMeeting(
        join_web_url=join_url,
        event_id=data.get("id") or None,
        subject=(data.get("subject") or "").strip() or None,
        start_datetime=start,
        end_datetime=end,
        web_link=data.get("webLink") or None,
    )
