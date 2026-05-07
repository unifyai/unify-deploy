# Webex — Meetings, Recordings, Transcripts

The decision/comms record half of the integration.  Most cross-app
value comes from joining meeting attendance to HubSpot contacts.

## Meetings

`list_webex_meetings(from_iso=..., to_iso=..., state=..., host_email=...)`

`state` values: `scheduled`, `inProgress`, `ended`, `missed`,
`expired`, `lobby`, `ready`, `active`.  Past meetings are `ended`.

Default lookback is 90 days when `from_iso` is omitted, configurable
via `WEBEX_MEETING_LOOKBACK_DAYS`.

`list_webex_meeting_invitees(meeting_id)` returns one row per invitee.
The `email` field is the join key for cross-app linking.

## Recordings

Recording **metadata** is mirrored.  Bytes are not.  Download URLs
have a short TTL (typically <1 day) — re-fetch via the live API when
needed:

```
get_webex_recording(recording_id, mock=False)
```

returns a fresh `downloadUrl` + `playbackUrl`.

## Transcripts — post-meeting only

Webex generates transcripts via ASR after the meeting ends.  Lag is
**5–30 minutes** depending on meeting length and load.  There is no
real-time transcript stream via the public API.

A transcript becomes available when its `status` is `available`.

### Mirror flag

By default the package mirrors transcript **metadata** but not body
text.  To mirror body text into `Webex/Transcripts.body`, set:

```
WEBEX_MIRROR_TRANSCRIPTS=true
```

Off-by-default rationale: transcript bodies can be sensitive
(operational chatter, named individuals) and high-volume.  Enable
explicitly per assistant when the use case warrants it.

When the mirror is off, fetch body text live on demand:

```
get_webex_meeting_transcript(transcript_id, format="txt", mock=False)
```

`format` is `"txt"` (plain) or `"vtt"` (timestamped).

## Cross-app joins

### Webex meetings ↔ HubSpot contacts

```
query_local_webex_meetings_with_hubspot_contacts(
    email="alex@example.test",   # OR
    meeting_id="...",
    from_iso="2026-04-01",
    to_iso="2026-05-01",
)
```

Joins `Webex/Meetings/Invitees.email` to
`HubSpot/CRM/Dimensions/Contacts.email` (lowercased).  Returns one row
per (meeting, invitee, hubspot_contact) triple with meeting title,
attendance role, and contact details.

Use cases: "Which HubSpot contacts attended Q2 ops reviews?", "What
meetings has this contact been invited to in the last 30 days?"

Requires both `webex` and `hubspot` packages to have run their syncs.

### Transcript ↔ Property keyword match

**Phase 2.**  Once RealPage data exists, transcripts will be scanned
for property keywords at sync time.  Until then, transcripts are
stored unjoined.

## Decision rules

- **"What meetings did X attend?"** → `query_local_webex_meetings_with_hubspot_contacts(email=X)`.
- **"Show me Alex's recordings from last week"** → `query_local_webex_recordings(host_email=...)`.
- **"What was discussed in meeting Y?"** → `query_local_webex_transcripts(meeting_id=Y)` if mirror is on, else `get_webex_meeting_transcript(transcript_id, mock=False)`.
- **"I need the recording video"** → `get_webex_recording(id, mock=False)` for a fresh `downloadUrl`.  Don't reuse the synced `download_url` field (TTL).
