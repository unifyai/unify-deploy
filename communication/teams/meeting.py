"""Look up Microsoft Teams online meetings via Graph.

Public surface: :func:`fetch_onlinemeeting_by_joinurl` + :class:`OnlineMeetingInfo`.

The browser-automation join flow (driven by Unity's agent service) only
needs the ``join_web_url`` to drive a Chromium tab into a meeting; this
helper exists so the meeting-creation endpoint can return a richer
metadata bundle (subject, organizer, expiry) without making the caller
deal with Graph response shapes directly.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OnlineMeetingInfo:
    """Subset of the Graph ``onlineMeeting`` payload we surface to callers.

    All fields except ``join_web_url`` are best-effort — Graph populates
    them inconsistently across mailbox SKUs and tenant configurations.
    """

    join_web_url: str
    meeting_id: Optional[str] = None
    subject: Optional[str] = None
    organizer_email: Optional[str] = None
    organizer_name: Optional[str] = None
    start_datetime: Optional[str] = None
    end_datetime: Optional[str] = None


def _strip_nondigits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _normalize_e164(raw: str) -> str:
    """Best-effort normalise a Graph-formatted phone number to E.164."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    prefix = "+" if raw.startswith("+") else ""
    return prefix + _strip_nondigits(raw)


async def fetch_onlinemeeting_by_joinurl(
    access_token: str,
    join_web_url: str,
    *,
    timeout_s: float = 15.0,
) -> Optional[OnlineMeetingInfo]:
    """Look up a Teams meeting by ``JoinWebUrl`` and return its metadata.

    Uses ``GET /me/onlineMeetings?$filter=JoinWebUrl eq '{url}'``.  Only
    the *organising* mailbox can resolve a meeting this way: Graph scopes
    ``/me/onlineMeetings`` to meetings owned by the calling user.

    Returns ``None`` when Graph returns an empty ``value`` array.  Raises
    :class:`PermissionError` on 401/403 so the caller can surface auth
    issues instead of silently degrading.
    """
    if not access_token or not join_web_url:
        return None

    escaped = join_web_url.replace("'", "''")

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(
            "https://graph.microsoft.com/v1.0/me/onlineMeetings",
            params={"$filter": f"JoinWebUrl eq '{escaped}'"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code in (401, 403):
        raise PermissionError(
            f"Graph /me/onlineMeetings rejected token: {resp.status_code} {resp.text}",
        )
    if resp.status_code != 200:
        logger.warning(
            "Graph /me/onlineMeetings returned %s for %s: %s",
            resp.status_code,
            join_web_url,
            resp.text,
        )
        return None

    items = (resp.json() or {}).get("value") or []
    if not items:
        return None
    meeting = items[0] or {}

    participants = meeting.get("participants") or {}
    org = (participants.get("organizer") or {}) if isinstance(participants, dict) else {}
    identity = (org.get("identity") or {}) if isinstance(org, dict) else {}
    user = identity.get("user") or {}
    organizer_name = (user.get("displayName") or "").strip() or None
    organizer_email = (org.get("upn") or "").strip() or None

    return OnlineMeetingInfo(
        join_web_url=meeting.get("joinWebUrl") or join_web_url,
        meeting_id=meeting.get("id") or None,
        subject=(meeting.get("subject") or "").strip() or None,
        organizer_email=organizer_email,
        organizer_name=organizer_name,
        start_datetime=meeting.get("startDateTime") or None,
        end_datetime=meeting.get("endDateTime") or None,
    )
