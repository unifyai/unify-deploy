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

## Function families

| Family | Files | Notes |
| --- | --- | --- |
| CRM core | `contacts`, `companies`, `deals`, `tickets`, `line_items`, `products`, `quotes` | Use `transition_deal_stage` for deal stage moves |
| Custom objects | `custom_objects` | Always call `discover_custom_object_schemas` first |
| Engagements | `engagement_calls/emails/meetings/notes/tasks` | Logging activity |
| Tier-gated | Sequences, Workflows, SMS, Custom Reports, Conversations | See `hubspot_tier_gating.md` |
| High-stakes writes | Send/publish/delete operations | See `hubspot_high_stakes_writes.md` |

Cross-app joins worth knowing: HubSpot contacts ↔ Webex meeting attendees
(via lowercased email).
