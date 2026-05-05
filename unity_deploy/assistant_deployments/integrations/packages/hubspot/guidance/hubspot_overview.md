# HubSpot Integration Overview

This package gives the assistant access to a customer's HubSpot CRM via two
complementary paths:

1. **Live HubSpot calls** — `get_*`, `search_*`, `list_*`, `create_*`,
   `update_*`, plus the higher-Hub functions (forms, sequences, KB
   articles, etc.).  These hit the HubSpot API directly using the customer's
   Private App token.
2. **Local DataManager queries** — `query_local_*` functions read a synced
   copy of the CRM kept in `HubSpot/CRM/...` contexts.  See
   `local_vs_live.md` for when to prefer which.

## Authentication

The customer creates a HubSpot Private App at Settings → Integrations →
Private Apps in their HubSpot account, picks the scopes their workflow
needs, and pastes the access token into the assistant's Secret Manager as
`HUBSPOT_PRIVATE_APP_TOKEN`.  Optional: `HUBSPOT_PORTAL_ID` for HubSpot UI
deep links.

If `HUBSPOT_PRIVATE_APP_TOKEN` is missing, all live functions return an
error envelope explaining what to set.  Direct the user to
https://developers.hubspot.com/docs/api/private-apps for setup steps.

## Mock mode

Every on-demand function defaults to `mock=True` and returns deterministic
fixture data without hitting HubSpot.  Pass `mock=False` to call the live
API.  Sync functions must run in real mode (`mock=False`) to populate the
DataManager copy.

## Core objects

| Object | Function file | Notes |
| --- | --- | --- |
| Contacts | `contacts.py` | People in the CRM |
| Companies | `companies.py` | Organizations |
| Deals | `deals.py` | Sales pipeline records (use `transition_deal_stage` for stage moves) |
| Tickets | `tickets.py` | Service tickets |
| Line items + Products | `line_items.py`, `products.py` | Catalog + per-deal lines |
| Quotes | `quotes.py` | Build + send quotes (high-stakes) |
| Custom objects | `custom_objects.py` | Always call `discover_custom_object_schemas` before working with custom records |
| Engagements | `engagement_*.py` | Calls, emails, meetings, notes, tasks |

## When to use what

- **Look someone up** → `query_local_contacts` first; fall through to `search_contacts(mock=False)` if local is stale.
- **Create or update** → live functions; mutations also mirror back to DataManager when `HUBSPOT_MIRROR_MUTATIONS_TO_DATAMANAGER=true` (default).
- **Log activity** → `log_call` / `log_email` / `log_meeting` / `create_note` / `create_task` with associations to the relevant records.
- **Send anything customer-visible** → see `high_stakes_writes.md`.

## Errors and tier gating

Many surfaces (Sequences, Workflows, Custom Reports, SMS) are gated on the
customer's HubSpot subscription tier.  When a tier-gated call returns 403,
the function returns an error envelope identifying the missing capability.
See `tier_gating.md` for handling guidance.
