# HubSpot Integration Overview

Reusable HubSpot connector covering CRM, Engagements, Marketing, Sales,
Service, CMS, and Analytics surfaces.

## Authentication

The customer creates a HubSpot Private App at Settings -> Integrations ->
Private Apps, picks scopes, and pastes the access token into Secret
Manager as `HUBSPOT_PRIVATE_APP_TOKEN`.  Optional: `HUBSPOT_PORTAL_ID`
unlocks UI deep links of the form
`https://app.hubspot.com/contacts/{portal_id}/contact/{id}`.

When the token is missing, live functions return a structured error
envelope; direct the user to
https://developers.hubspot.com/docs/api/private-apps.

## How to use

- **Live reads + writes** — call `hubspot_request(method, path, params,
  body)` for any HubSpot REST endpoint.  First-page only; bulk pulls go
  through the sync orchestrator.  4xx envelopes carry hints (e.g.
  tier-gated 403, missing scope) — read them.
- **CRM search** — `search_hubspot(query, object_types, limit_per_type)`
  is a typed wrapper because `POST /crm/v3/objects/{type}/search` has
  non-trivial `filterGroups` / `sorts` / `properties` semantics.  Use
  it instead of constructing the search body by hand.
- **Associations** — `list_hubspot_associations`,
  `create_hubspot_association`, `delete_hubspot_association` cover the
  v4 association API; the typed wrappers handle the association-types
  catalog.
- **Properties metadata** — `list_hubspot_properties`,
  `get_hubspot_property`, `create_hubspot_property` for schema
  discovery and custom-property creation.
- **Bulk / snapshot** — `run_hubspot_sync_tick(...)` mirrors CRM,
  Engagements, Marketing, Sales, Service, CMS, and Platform surfaces
  into DataManager.
- **Local analytics** — once synced, prefer `query_local_hubspot_*`
  helpers over re-hitting the API.

## Tier gating

Sequences, Workflows, SMS, Custom Reports, Conversations, and a few
others are tier-gated on HubSpot's side.  A 403 from `hubspot_request`
on those endpoints means the customer's plan or Private App scopes
don't cover that surface — surface the hint to the user; do not retry
blindly.  See `hubspot_tier_gating.md`.

## High-stakes writes

`hubspot_request` supports POST/PATCH/PUT/DELETE.  There is **no code
gate** — the assistant must surface what's about to change to the user
before issuing a destructive verb (sending email, publishing content,
deleting records, enrolling contacts in workflows, etc.).  See
`hubspot_high_stakes_writes.md` for the full list of surfaces that
warrant explicit confirmation.

## Cross-app joins

HubSpot contacts ↔ Webex meeting attendees (via lowercased email).
HubSpot contacts ↔ Matterport view events (via `utm_email` URL
instrumentation; see Matterport's `correlate_matterport_views_to_hubspot_leads`).
