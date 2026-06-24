# Microsoft Teams Meetings

This document covers the two Teams-meeting capabilities in the
communication service:

1. **Creation** — `POST /teams/create_meeting` (instant or scheduled).
2. **Join** — driven by Unity's agent service via browser automation
   (out of scope here; this service exposes only the join URL).

> The legacy PSTN dial-in bridge (`/teams/join_meeting` over Twilio +
> Audio Conferencing) has been retired. Joining now uses the same
> browser-automation stack as Google Meet, which gives us screen share,
> active-speaker identification, and works for externally-linked
> mailboxes (no tenant-wide admin consent required).

---

## Creating a meeting

`POST /teams/create_meeting` wraps two Microsoft Graph endpoints behind
a single request shape. The mode is selected by the `mode` field.

### Authentication

- Endpoint requires the standard `Authorization: Bearer
  <ORCHESTRA_ADMIN_KEY>` header.
- The assistant identified by `assistant_email` must have a delegated
  `MICROSOFT_ACCESS_TOKEN` stored in Orchestra. Provision via the BYOD
  OAuth flow with at minimum:
  - `OnlineMeetings.ReadWrite` (instant + scheduled)
  - `Calendars.ReadWrite` (scheduled only — `POST /me/events`)

If the token is missing, the endpoint returns **409 Conflict** with a
remediation hint.

### Mode: `instant`

Calls Graph `POST /me/onlineMeetings`. Returns a join URL with no
calendar entry. `subject`, `start`, `end` are all optional.

```json
POST /teams/create_meeting
{
  "assistant_email": "assistant@contoso.com",
  "mode": "instant",
  "subject": "Quick sync"
}
```

Response:

```json
{
  "success": true,
  "join_web_url": "https://teams.microsoft.com/l/meetup-join/...",
  "meeting_id": "MSpkYzU0...",
  "event_id": null,
  "subject": "Quick sync",
  "start": null,
  "end": null,
  "web_link": null
}
```

### Mode: `scheduled`

Calls Graph `POST /me/events` with `isOnlineMeeting=true` and
`onlineMeetingProvider="teamsForBusiness"`. Creates a real calendar
entry with attendees; Graph attaches a Teams meeting and returns its
`joinUrl`.

`subject`, `start`, `end` are required. `start`/`end` are ISO-8601
strings interpreted in `timezone` (defaults to `"UTC"`).

```json
POST /teams/create_meeting
{
  "assistant_email": "assistant@contoso.com",
  "mode": "scheduled",
  "subject": "Quarterly review",
  "start": "2026-05-01T15:00:00",
  "end": "2026-05-01T16:00:00",
  "timezone": "UTC",
  "attendees": ["alice@example.com", "bob@example.com"],
  "body": "<p>Agenda: Q1 results.</p>",
  "location": "Online"
}
```

Response includes `event_id` (Graph event id) and `web_link` (Outlook
deep link to the calendar entry):

```json
{
  "success": true,
  "join_web_url": "https://teams.microsoft.com/l/meetup-join/...",
  "meeting_id": null,
  "event_id": "AAMkAG...",
  "subject": "Quarterly review",
  "start": "2026-05-01T15:00:00.0000000",
  "end":   "2026-05-01T16:00:00.0000000",
  "web_link": "https://outlook.office.com/owa/?itemid=..."
}
```

### Pub/Sub event

On success, the endpoint publishes `thread: "teams_meet_created"` to the
assistant's Pub/Sub topic with the meeting metadata. Unity's
`ConversationManager` listens for this thread to record the join URL
against the assistant's task graph.

### Errors

| Status | Cause |
|--------|-------|
| 400    | Missing `assistant_email`, invalid `mode`, or scheduled mode missing `subject`/`start`/`end` |
| 403    | Graph rejected the token (`PermissionError` from the helper) |
| 409    | Assistant has no `MICROSOFT_ACCESS_TOKEN` |
| 502    | Graph returned a non-401/403 error or returned a meeting without `joinWebUrl`/`joinUrl` |

---

## Joining a meeting

Joining is handled by Unity's **agent service** (Playwright + virtual
display + virtual audio devices), the same stack that joins Google Meet.
This service does **not** expose a `/teams/join_meeting` endpoint:
Unity reads the `join_web_url` (either from this endpoint's response or
from a parsed invite) and drives a browser into the meeting.

What the browser flow gets us that PSTN dial-in could not:

- **Screen share** — the assistant can present the desktop URL view
  (same approach as Google Meet).
- **Active-speaker detection** — DOM scraping of the participants
  panel, correlated with Deepgram diarization on the meeting audio.
- **Compatibility with external/BYOD mailboxes** — no tenant-wide
  admin consent required (calling-bots would have).

See Unity's `agent-service` documentation for the join API and the
shared browser-automation primitives.

---

## Meeting metadata lookup

`communication/teams/meeting.py::fetch_onlinemeeting_by_joinurl` is a
slim helper around Graph `GET /me/onlineMeetings?$filter=JoinWebUrl eq
'...'`. It returns the meeting's subject, organizer, and start/end times
when the calling mailbox is the organizer. Used internally by callers
that have a join URL but need richer metadata; not exposed as an HTTP
endpoint.
