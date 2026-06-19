# Webex Integration Package

Generic, reusable Cisco Webex connector covering:

- **Meetings** — list / get scheduled and past meetings + invitees.
- **Recordings** — list / get recording metadata (download URLs).
- **Transcripts** — post-meeting transcripts with optional DataManager
  mirror behind `WEBEX_MIRROR_TRANSCRIPTS=true`.
- **People & Rooms** — directory + spaces + memberships.
- **Incremental sync** — single-tick orchestrator over the above with
  per-object cadence gating.
- **Local query** — DataManager-backed analytical reads, including
  cross-joins to HubSpot contacts (by attendee email).

Authenticated via OAuth 2.0 (authorization code flow).  The customer
registers a **Webex Integration** at <https://developer.webex.com>,
pastes its Client ID + Client Secret into Console -> Integrations,
clicks **Connect**, and grants consent on Webex.  The OAuth callback
writes the refresh token automatically; the runtime mints short-lived
access tokens from there.

On-demand functions default to `mock=True` for safe testing; sync
functions require a live token.  403 responses are returned as
graceful error envelopes so capability gating is uniform across the
package.

For setup, see `guidance/webex_setup.md`.
