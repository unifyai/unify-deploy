# Matterport Integration — Overview

3D virtual-tour analytics connector for Matterport-scanned spaces.  Used
when:

- Listing or fetching 3D models for the customer's properties.
- Reading per-model engagement (view counts, unique visitors,
  time-on-tour, top referrers).
- Generating a Showcase URL the user can embed or share.
- Linking a model to a record in another system (e.g. a unit, listing,
  project site), or correlating tour views to leads in a CRM
  (`matterport_lead_correlation.md`).

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
`generate_matterport_embed_url`.  If the question is "how engaged are
leads," use the view-stat functions.  Default Showcase URLs are
public/unlisted — no signed-token API needed for share or embed.  See
`matterport_embed_vs_analytics.md`.

## Tier gating

Sandbox tokens only see Matterport's demo models.  Production access
against the customer's real models requires the **Developer Tools
add-on** on their Matterport plan.  See `matterport_tier_gating.md`.
