# Webex Integration — Overview

Reusable Cisco Webex connector covering meetings, recordings,
post-meeting transcripts, rooms (spaces), and people.  Used when:

- Looking up scheduled or past meetings, attendees, recordings, or
  transcripts.
- Cross-referencing meeting attendees with CRM contacts (deal
  attribution, decision records).
- Reading Webex room/space membership and recent activity.

## Authentication

OAuth 2.0 (authorization code flow).  Customer registers a Webex
Integration app at https://developer.webex.com and pastes its
`WEBEX_OAUTH_CLIENT_ID` + `WEBEX_OAUTH_CLIENT_SECRET` into Console ->
Settings -> Secrets.  Connecting in Console -> Integrations runs the
OAuth round-trip and writes `WEBEX_REFRESH_TOKEN`.  See
`webex_setup.md`.

Access tokens are 14-day TTL; refresh tokens 90-day TTL.  Runtime mints
short-lived access tokens behind the scenes.  Scopes the dev-app
declares determine which capabilities are usable; gaps surface as 403
envelopes.

## What's NOT integrated (yet)

- **Writes** — schedule/cancel meetings, post messages.  Phase 2.
- **Messages** — message bodies are not mirrored by default.  Privacy +
  volume.
- **Recording bytes** — only metadata + short-lived download URLs.
- **Real-time transcripts** — only post-meeting transcripts (5-30 min
  ASR lag).

## Cross-app joins

`query_local_webex_meetings_with_hubspot_contacts` — joins meeting
invitees (by lowercased email) to HubSpot contacts.  Use for "which
HubSpot contacts attended which Webex meetings."  Requires both
packages to have run their syncs.
