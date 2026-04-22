"""Resolve PSTN dial-in details for a Microsoft Teams meeting.

Two resolution sources, tried in order:

1. **Graph API** (``GET /me/onlineMeetings?$filter=JoinWebUrl eq '{url}'``).
   Requires a delegated access token with ``OnlineMeetings.Read`` and the
   organising mailbox to be the one holding the token — Graph only
   returns meetings in the *calling user's* mailbox.  When the assistant
   mailbox is a *participant* (not the organiser) or was invited via
   Bcc/forwarded link, Graph returns ``404`` and we fall through.
2. **Regex over the invite body** (Outlook invite HTML / plain text).
   All Microsoft-generated invites contain the canonical dial-in block

       _________________________________________________________________________________

       Microsoft Teams meeting
       Join on your computer, mobile app or room device
       <...>
       Or call in (audio only)
       +1 323-555-0123,,987654321#   United States, Los Angeles
       Phone Conference ID: 987 654 321#

   The phone-comma-comma-digits form and the ``Phone Conference ID`` line
   are both present in every language variant; we match the latter
   because it's language-robust (``Phone Conference ID`` stays in English
   by default in Microsoft's invite template even when the rest is
   localised).

Public surface: ``resolve_meeting_dialin(...)`` + ``MeetingDialIn``.  The
Teams views endpoint consumes these; no other caller should.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MeetingDialIn:
    """Resolved dial-in information for a Teams meeting.

    ``dial_in_number`` is E.164 (``+1...``), ``conference_id`` is
    digits-only (no whitespace, no ``#``).  ``source`` is one of
    ``"graph"`` | ``"invite_body"`` | ``"manual"``.

    ``organizer_email`` / ``organizer_name`` / ``meeting_subject`` are
    populated only when Graph resolves the meeting (the invite-body
    path doesn't give us reliable organizer metadata and we treat the
    boss as a safe default upstream when these are missing).
    """

    dial_in_number: str
    conference_id: str
    source: str
    toll_free_number: Optional[str] = None
    organizer_email: Optional[str] = None
    organizer_name: Optional[str] = None
    meeting_subject: Optional[str] = None


# ---------------------------------------------------------------------------
# Invite-body regexes
# ---------------------------------------------------------------------------

# "Phone Conference ID: 987 654 321 #"  — allow any whitespace between digits.
_CONFERENCE_ID_RE = re.compile(
    r"Phone\s+Conference\s+ID\s*:\s*([\d\s]{5,})#?",
    re.IGNORECASE,
)

# "+1 323-555-0123,,987654321#" — pull the country-prefixed number preceding
# the double-comma marker.  Keep digits, ``+``, spaces, dashes, parens.
_INLINE_NUMBER_RE = re.compile(
    r"(\+[\d][\d\s\-\(\)]{6,})\s*,\s*,\s*(\d[\d\s]{4,})#",
)

# Fallback: any E.164 near the literal "audio only" phrase Microsoft uses
# in every localised invite.
_AUDIO_ONLY_LINE_RE = re.compile(
    r"audio\s*only.*?(\+[\d][\d\s\-\(\)]{6,})",
    re.IGNORECASE | re.DOTALL,
)


def _strip_nondigits(s: str) -> str:
    """Return only the digit characters from *s*."""
    return re.sub(r"\D", "", s or "")


def _normalize_e164(raw: str) -> str:
    """Best-effort normalise an invite-formatted phone number to E.164.

    Returns the input stripped to ``+<digits>``.  Invite templates use
    spaces, dashes, and parens liberally; those are all decorative.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    prefix = "+" if raw.startswith("+") else ""
    return prefix + _strip_nondigits(raw)


def parse_dialin_from_invite(invite_body: str) -> Optional[MeetingDialIn]:
    """Extract dial-in number + conference ID from a raw invite body.

    *invite_body* may be HTML or plain text.  Returns ``None`` when we
    can't locate both pieces; callers should fall back to Graph or fail.
    """
    if not invite_body:
        return None

    # Strip HTML tags cheaply; keep text content.  Good enough for Outlook
    # invites because the relevant lines are never wrapped in complex
    # markup that would merge digits across tag boundaries.
    text = re.sub(r"<[^>]+>", " ", invite_body)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)

    inline = _INLINE_NUMBER_RE.search(text)
    if inline:
        number = _normalize_e164(inline.group(1))
        conf = _strip_nondigits(inline.group(2))
        if number.startswith("+") and conf:
            return MeetingDialIn(
                dial_in_number=number,
                conference_id=conf,
                source="invite_body",
            )

    conf_match = _CONFERENCE_ID_RE.search(text)
    num_match = _AUDIO_ONLY_LINE_RE.search(text)
    if conf_match and num_match:
        number = _normalize_e164(num_match.group(1))
        conf = _strip_nondigits(conf_match.group(1))
        if number.startswith("+") and conf:
            return MeetingDialIn(
                dial_in_number=number,
                conference_id=conf,
                source="invite_body",
            )
    return None


# ---------------------------------------------------------------------------
# Graph lookup
# ---------------------------------------------------------------------------


async def fetch_dialin_from_graph(
    access_token: str,
    join_web_url: str,
    *,
    timeout_s: float = 15.0,
) -> Optional[MeetingDialIn]:
    """Look up a Teams meeting by ``JoinWebUrl`` and return its dial-in.

    Uses a raw HTTPS call to ``/me/onlineMeetings?$filter=JoinWebUrl eq
    '{url}'`` because the Graph Python SDK's ``OData filter`` plumbing is
    noisier than a one-shot request for a single string match.

    Returns ``None`` when Graph returns an empty ``value`` array or when
    ``audioConferencing`` is absent (tenants without an Audio
    Conferencing SKU don't populate it).  Raises only on non-200
    responses the caller should surface (401/403).
    """
    if not access_token or not join_web_url:
        return None

    # Graph's $filter requires the URL to be surrounded by single quotes
    # and any embedded single quotes doubled.  Join URLs don't normally
    # contain ``'`` but be defensive.
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

    data = resp.json()
    items = data.get("value") or []
    if not items:
        return None

    meeting = items[0] or {}
    audio = meeting.get("audioConferencing") or {}
    conference_id = _strip_nondigits(audio.get("conferenceId") or "")
    toll = _normalize_e164(audio.get("tollNumber") or "")
    toll_free = _normalize_e164(audio.get("tollFreeNumber") or "")
    number = toll or toll_free
    if not conference_id or not number:
        return None

    # Graph returns the organizer as a ``MeetingParticipantInfo`` with an
    # ``identity`` -> ``user`` shape (``id``, ``displayName``) and a
    # top-level ``upn`` at the participant level.  None of these are
    # guaranteed, so treat the whole block as best-effort.
    participants = meeting.get("participants") or {}
    org = (
        (participants.get("organizer") or {}) if isinstance(participants, dict) else {}
    )
    identity = (org.get("identity") or {}) if isinstance(org, dict) else {}
    user = identity.get("user") or {}
    organizer_name = (user.get("displayName") or "").strip() or None
    organizer_email = (org.get("upn") or "").strip() or None
    meeting_subject = (meeting.get("subject") or "").strip() or None

    return MeetingDialIn(
        dial_in_number=number,
        conference_id=conference_id,
        source="graph",
        toll_free_number=toll_free or None,
        organizer_email=organizer_email,
        organizer_name=organizer_name,
        meeting_subject=meeting_subject,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def resolve_meeting_dialin(
    *,
    access_token: Optional[str],
    join_web_url: Optional[str],
    invite_body: Optional[str] = None,
    manual_dial_in_number: Optional[str] = None,
    manual_conference_id: Optional[str] = None,
) -> Optional[MeetingDialIn]:
    """Resolve a dial-in using (in order) manual > Graph > invite body.

    Manual values win because the caller is taking explicit responsibility
    — e.g. an externally-linked mailbox pasting a dial-in from the
    browser meeting lobby when ``onlineMeetings`` isn't reachable.
    """
    if manual_dial_in_number and manual_conference_id:
        return MeetingDialIn(
            dial_in_number=_normalize_e164(manual_dial_in_number),
            conference_id=_strip_nondigits(manual_conference_id),
            source="manual",
        )

    if access_token and join_web_url:
        try:
            graph_hit = await fetch_dialin_from_graph(access_token, join_web_url)
        except PermissionError as e:
            logger.info("Graph lookup forbidden, falling back to invite body: %s", e)
            graph_hit = None
        if graph_hit:
            return graph_hit

    if invite_body:
        return parse_dialin_from_invite(invite_body)

    return None
