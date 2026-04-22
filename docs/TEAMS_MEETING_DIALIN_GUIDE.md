# Teams Meeting PSTN Dial-in Guide

This document covers the Teams-meeting PSTN dial-in bridge in the
Communication service: architecture, OAuth scopes, runtime settings,
the Pub/Sub contract consumed by Unity, and the failure modes to watch
for in production.

## What It Does

Joins a Microsoft Teams meeting as an **audio-only participant** by:

1. Resolving the meeting's **Audio Conferencing dial-in number** +
   **conference ID** (Graph API, invite-body parsing, or manual
   override).
2. Originating an outbound Twilio call from the assistant's Twilio DID
   **to the LiveKit SIP URI** — the AI voice agent is already
   dispatched in the LiveKit room and receives the SIP leg.
3. Returning TwiML that tells Twilio to forward the call to the
   meeting's PSTN number and DTMF the conference ID + `#`.

The result: exactly the same topology as a regular outbound phone call
(one Twilio leg, one LiveKit room, one voice agent), with Teams simply
acting as the "far-end" PSTN endpoint.

## Why PSTN Dial-in (and Not a Calling Bot)

Teams **calling bots** require tenant-wide admin consent and a
published Calling Bot application — a blocker for:

- assistants on externally-linked Microsoft 365 mailboxes (consumer
  accounts, other tenants, Outlook.com)
- fast iteration / per-user BYOD flows

**Audio Conferencing** is on every E-series SKU with Teams Phone and
is available to anyone with the meeting's dial-in details. The
trade-off is **audio only** (no video, no screen share, no in-meeting
chat) — which matches how a voice assistant interacts with a meeting
anyway.

## Dial-in Resolution

`communication/teams/meeting.py::resolve_meeting_dialin` tries three
sources in order:

1. **Manual override** — caller passes `dial_in_number` + `conference_id`
   directly (e.g. when the UI extracts them from the meeting lobby).
2. **Graph** — `GET /me/onlineMeetings?$filter=JoinWebUrl eq '...'`.
   Requires the organising mailbox's delegated access token to hold
   the `OnlineMeetings.Read` scope. Extracts:
   - `audioConferencing.tollNumber` → dial-in
   - `audioConferencing.conferenceId` → DTMF sequence
   - `subject`, `participants.organizer.identity.user.displayName`,
     `participants.organizer.upn` → metadata for the opening prompt
3. **Invite body regex** — parses the canonical Microsoft invite block:

   ```
   Or call in (audio only)
   +1 323-555-0123,,987654321#   United States, Los Angeles
   Phone Conference ID: 987 654 321
   ```

   Language-robust because `Phone Conference ID` stays in English in
   Microsoft's invite template by default.

All three produce a `MeetingDialIn` dataclass with a `source` tag
(`"manual"` / `"graph"` / `"invite_body"`) for observability.

## OAuth Scope

The Teams Microsoft-365 scope bundle (`common/scopes.py`) includes:

```
OnlineMeetings.Read
```

Grant this alongside the existing Teams scopes via the BYOD / tenant
admin consent flow. Without it Graph returns `403` and we fall
through to invite-body parsing.

## Runtime Settings

| Env var | Default | Purpose |
|---|---|---|
| `TEAMS_CONFERENCING_IVR_PAUSE_S` | `8` | Number of 0.5s DTMF waits (`w`) prepended to the conference ID. `8` = ~4s, enough to clear Microsoft's default Audio Conferencing greeting on the English prompt. Tune upward if a tenant has a long custom branding greeting. |
| `TEAMS_CONFERENCING_CALLER_ID` | `""` | Optional Twilio E.164 to use as the outbound caller ID. Defaults to the assistant's own Twilio DID so callerID matches the assistant's identity end-to-end. |

## Endpoints

### `POST /teams/join_meeting`

Request:

```json
{
  "assistant_email": "astra@contoso.com",
  "join_web_url":    "https://teams.microsoft.com/l/meetup-join/...",
  "invite_body":     "... (optional fallback) ...",
  "dial_in_number":  "+13235550123",     // optional manual override
  "conference_id":   "987654321",        // optional manual override
  "subject":         "1:1 with Alice",   // optional, for logging
  "record":          false               // optional, LiveKit egress to GCS
}
```

Response:

```json
{
  "success":         true,
  "call_sid":        "CAxxxx",
  "conference_name": "Unity_TeamsMeet_42_20260420_163700",
  "room_name":       "unity_42_teams_meet",
  "dial_in_source":  "graph",
  "dial_in_number":  "+13235550123",
  "conference_id":   "987654321"
}
```

### `POST /teams/leave_meeting`

Accepts either identifier (pick one):

```json
{ "call_sid":        "CAxxxx" }
{ "conference_name": "Unity_TeamsMeet_42_20260420_163700" }
```

When only `conference_name` is supplied the endpoint scans in-progress
Twilio calls whose status-callback URL points at
`/twilio/teams-meet-call-status?...conference_name=...` and hangs up
the match. Returns success when no active call is found (idempotent —
the post-condition "this meeting is no longer live" is satisfied).

### `POST /phone/teams-meet-twiml` (unauthenticated)

Twilio fetches this during call origination. Returns TwiML that dials
the meeting's PSTN number and auto-DTMFs the conference ID. Not
intended to be called by clients.

### `POST /twilio/teams-meet-call-status` (adapters service, Twilio-signed)

Dedicated status callback for the Twilio leg bridging an assistant
into a Teams meeting. `/teams/join_meeting` points Twilio at this URL
(with `?assistant_id=...&livekit_room=...&conference_name=...`) so
the generic `/twilio/call-status` stays focused on regular phone
calls. The endpoint:

- Routes through `build_webhook_context(channel="teams_meet",
  ensure_job=True, validate_contact=False)` to activate and keep the
  Unity container warm across the meeting lifetime.
- Publishes `teams_meet_started` on `in-progress` and
  `teams_meet_ended` on any terminal status (`completed`,
  `no-answer`, `busy`, `canceled`, `failed`).
- Carries boss-only contacts — see the Pub/Sub contract below.

## Shared Helpers

- `common.contacts.build_boss_contact(assistant)` — constructs the
  canonical `contact_id=1` envelope for outbound-initiated voice
  sessions that can't match a phone number or email to a known
  contact (Teams-meet dial-in). Used by `/teams/join_meeting` and
  `/twilio/teams-meet-call-status`; swapping both over to the shared
  helper keeps the contact shape consistent and removes the
  duplicate inline builder.
- `common.pubsub.publish_assistant_event(assistant_id, thread, event)`
  — wraps the assistant's Pub/Sub topic path, the canonical envelope
  (`{thread, publish_timestamp, event}`), and the `thread` message
  attribute in a single call. Both the join-time publish and the
  call-status publish go through this helper.

## Pub/Sub Contract (Consumed by Unity)

Three event threads are published to the assistant's Pub/Sub topic
(`unity-{assistant_id}{env_suffix}`). All use the standard envelope:

```json
{
  "thread":            "...",
  "publish_timestamp": 1712345678.123,
  "event":             { ...payload... }
}
```

### `thread: "teams_meet"` (published by `/teams/join_meeting`)

Emitted **immediately on successful originate** (before the PSTN leg
connects), so Unity's `comms_manager` can spin up the voice session
and have the agent ready when SIP audio arrives.

```json
{
  "contacts":         [ { "contact_id": 1, ... } ],
  "livekit_room":     "unity_42_teams_meet",
  "conference_name":  "Unity_TeamsMeet_42_20260420_163700",
  "twilio_call_sid":  "CAxxxx",
  "assistant_email":  "astra@contoso.com",
  "call_metadata": {
    "join_web_url":    "https://teams.microsoft.com/l/meetup-join/...",
    "meeting_subject": "Quarterly review",
    "organizer_name":  "Alice Smith",
    "organizer_email": "alice@contoso.com",
    "dial_in_number":  "+13235550123",
    "conference_id":   "987654321",
    "dial_in_source":  "graph"
  }
}
```

Contacts carries **boss only** (`contact_id=1`) — Teams-meet is
outbound-initiated and the assistant entry (`contact_id=0`) must never
appear, because Unity's `comms_manager` teams-meet branch picks the
first **non-boss** contact as "organizer" and would otherwise pick the
assistant themselves. Unity then uses the organizer metadata in
`call_metadata` to seed the voice agent's opening prompt.

### `thread: "teams_meet_started"` (published by `/twilio/teams-meet-call-status`)

Emitted when Twilio's status callback reports `CallStatus=in-progress`
on the Teams-meet leg. The dedicated adapter endpoint routes through
`build_webhook_context(channel="teams_meet", ensure_job=True)` so the
Unity container is activated and kept warm for the full meeting — a
plain publish would let the container idle-recycle mid-meeting and
drop the `teams_meet_ended` event.

```json
{
  "contacts":         [ { "contact_id": 1, ... } ],
  "assistant_id":     "42",
  "livekit_room":     "unity_42_teams_meet",
  "conference_name":  "Unity_TeamsMeet_42_20260420_163700",
  "twilio_call_sid":  "CAxxxx",
  "call_status":      "in-progress",
  "timestamp":        1712345678123
}
```

### `thread: "teams_meet_ended"` (published by `/twilio/teams-meet-call-status`)

Emitted on any terminal status (`completed`, `no-answer`, `busy`,
`canceled`, `failed`). Same event payload as `teams_meet_started`
with the terminal `call_status`. Intermediate statuses (`queued`,
`ringing`, `initiated`) are dropped — Twilio re-fires on the next
transition.

## Architecture Diagram

```
 ┌────────────────────┐
 │ Unity brain action │  join_teams_meet(join_web_url, context, ...)
 └─────────┬──────────┘
           │ HTTP
           ▼
 ┌──────────────────────────────┐
 │ Communication service        │
 │ /teams/join_meeting          │──┐ Graph /onlineMeetings (OnlineMeetings.Read)
 │                              │──┘ or invite-body regex or manual
 │                              │
 │ 1. Resolve dial-in           │
 │ 2. Create LiveKit room +     │   ┌──────────────────┐
 │    dispatch voice agent      ├──▶│ LiveKit + agent  │
 │ 3. Originate Twilio call     │   └──────────────────┘
 │    to sip:<DID>@livekit-sip  │
 │    with TwiML URL that dials │
 │    the meeting's PSTN bridge │
 │ 4. Publish thread:teams_meet │───▶ Pub/Sub → Unity comms_manager
 └────┬─────────────────────────┘
      │ Twilio originate
      ▼
 ┌────────────┐     TwiML:
 │ Twilio     │ ───▶ <Dial><Number sendDigits="wwwww<conf>#">
 │ (carrier)  │      +1 323-555-0123 (Teams PSTN)
 └─────┬──────┘
       │ SIP                                   │ PSTN
       ▼                                       ▼
 ┌──────────────────┐                   ┌────────────────────┐
 │ LiveKit SIP      │ ◀── audio bridge ▶│ Microsoft Audio    │
 │ (room = "teams_  │                   │ Conferencing bridge│
 │  meet")          │                   │ → Teams meeting    │
 └────────┬─────────┘                   └────────────────────┘
          │ media
          ▼
 ┌───────────────────┐
 │ Voice agent       │
 │ (LLM, TTS, STT)   │
 └───────────────────┘


     Twilio ──POST──▶ /twilio/teams-meet-call-status?...
     (CallStatus transitions)                   │
                                                ▼
                              build_webhook_context(channel=
                              "teams_meet", ensure_job=True)
                                                │
                                                ▼
                             publish thread:teams_meet_started /
                                       teams_meet_ended
                                                │
                                                ▼
                                     Unity comms_manager
```

## Failure Modes & Mitigations

| Symptom | Likely cause | Mitigation |
|---|---|---|
| `422 Could not resolve dial-in` | Meeting has no Audio Conferencing SKU, token lacks `OnlineMeetings.Read`, and no invite body | Enable Audio Conferencing on the tenant, grant the scope, or call with `invite_body` / manual override |
| Bridge connects but conference ID gets rejected ("We didn't recognise that conference ID") | `TEAMS_CONFERENCING_IVR_PAUSE_S` too short — DTMF sent during the greeting | Increase env var; 8 (~4s) is default, bump to 12–16 for tenants with custom greetings |
| Bridge connects but is silent | LiveKit phone dispatch rule not created for the room name | Check `ensure_phone_dispatch_rule` logs; the helper is idempotent, rerunning the endpoint fixes |
| `502` from Twilio on originate | Unverified caller ID on trial accounts | Provision a production Twilio subaccount, or set `TEAMS_CONFERENCING_CALLER_ID` to a verified number |
| `teams_meet_started` never fires | Twilio status callback can't reach `UNITY_ADAPTERS_URL` | Ensure the adapters service is publicly reachable; check Twilio debugger for webhook errors |
| No Pub/Sub event on join success | Topic doesn't exist or publisher lacks IAM | Publishes are non-fatal — call goes through; inspect communication-service logs for the warning |

## Testing

Unit test suites:

- `tests/teams/test_meeting_dialin.py` — `resolve_meeting_dialin`, Graph parsing, invite-body regexes
- `tests/teams/test_join_meeting.py` — `/teams/join_meeting` + `/teams/leave_meeting` happy paths, error branches, Pub/Sub payload contract
- `tests/phone/test_teams_meet_bridge.py` — TwiML shape, `/phone/teams-meet-twiml` endpoint, Twilio originate URL encoding
- `tests/common/test_pubsub.py` — `publish_assistant_event` envelope shape
- `tests/common/test_contacts.py` — `build_boss_contact` shape + `contact_id=1` invariant
- `tests/adapters/test_teams_meet_call_status.py` — `/twilio/teams-meet-call-status` endpoint routing, `build_webhook_context` wiring, started / ended / intermediate-skipped, publish-failure resilience

Run locally:

```bash
conda activate orchestra
python3 -m pytest tests/teams tests/phone/test_teams_meet_bridge.py \
    tests/common/test_pubsub.py tests/adapters/test_teams_meet_call_status.py
```
