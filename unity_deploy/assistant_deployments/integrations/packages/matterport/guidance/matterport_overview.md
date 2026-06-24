# Matterport Integration — Overview

3D virtual-tour analytics connector for Matterport-scanned spaces.  Used
when:

- Listing or fetching 3D models for the customer's properties.
- Reading per-model engagement (view counts, unique visitors,
  time-on-tour, top referrers).
- Generating a Showcase URL the user can embed or share.
- Linking a model to a record in another system (e.g. a unit, listing,
  project site), or correlating tour views to leads in a CRM.

## How to use

- **Live reads** — call `matterport_graphql_query(query, variables)`
  for any Model API GraphQL query.  Inspect the live schema first;
  Matterport's schema evolves and not every documented field is
  available on every plan tier.  4xx envelopes carry hints (e.g.
  tier-gated 403, sandbox-only token) — read them.
- **Bulk / snapshot** — `run_matterport_sync_tick(...)` mirrors models
  and per-model view stats into DataManager on each tick.
- **Local analytics** — once synced, prefer `query_local_matterport_*`
  helpers over re-hitting the API.
- **Cross-app joins** — `link_matterport_model_to_unit`,
  `lookup_matterport_model_for_unit`, and
  `correlate_matterport_views_to_hubspot_leads` are typed helpers
  built on top of DataManager (no live calls).
- **Embed** — `generate_matterport_embed_url(model_id, options)` is
  pure URL construction; no API call.

## Authentication

**HTTP Basic with an API token pair — not OAuth.**  The Token ID +
Token Secret *are* the credentials; send them directly via HTTP Basic
on every call.  Do **not** POST to `/api/oauth/token`, do **not**
exchange them for a Bearer access token, do **not** treat them as
OAuth `client_id` / `client_secret`.  Matterport's OAuth endpoint will
return `401 invalid_client` for these tokens because they aren't OAuth
client credentials — that error means you took the wrong path, not
that the credentials are bad.

Customer generates an API Token at Matterport Settings -> Account ->
API Access and pastes **Token ID** + **Token Secret** into Console ->
Integrations -> Matterport.  No OAuth callback.  See
`matterport_setup.md`.

## Embed vs analytics

If the question is "show me the tour," use
`generate_matterport_embed_url` — no API call needed; default
Showcase URLs are public/unlisted.  If the question is "how engaged
are leads," prefer `query_local_matterport_view_stats` against the
synced copy, or fall through to `matterport_graphql_query` for ad-hoc
windows the sync hasn't covered.

## Tier gating

Sandbox tokens only see Matterport's demo models.  Production access
against the customer's real models requires the **Developer Tools
add-on** on their Matterport plan.  See `matterport_tier_gating.md`.
