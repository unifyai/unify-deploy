# HubSpot Integration

Generic, reusable HubSpot connector covering CRM Hub, Engagements, Marketing
Hub, Sales Hub, Service Hub, CMS Hub, Platform, and Analytics surfaces. Lives
under `integrations/packages/` so any client can opt in via `integrations:
["hubspot"]` on their deployment spec.

## What this package provides

* **On-demand functions** — get/list/search/create/update across every CRM
  object plus the higher-Hub APIs. Each function defaults to `mock=True` for
  safe testing and returns deterministic fixture data; pass `mock=False` to
  hit the live HubSpot API.
* **Sync functions** — pull deltas from HubSpot into the assistant's
  DataManager. Returns the canonical
  `{schema_version, tables, metadata}` envelope for DataManager ingest.
* **Local-query functions** — read the synced DataManager copy. Preferred
  over live calls when freshness allows.
* **Sync orchestrator** — `run_hubspot_sync_tick` aggregates all enabled
  object types into one envelope and gates per-object cadence by config.
* **Tier detection** — `_capabilities.probe_tier()` discovers which HubSpot
  Hubs the portal is subscribed to. Tier-gated functions return graceful
  errors rather than raising on 403.
* **Guidance** — eighteen markdown files cover every Hub plus
  cross-cutting concerns (tier gating, high-stakes writes, local vs. live,
  the sync runbook).

## Configuration

The customer adds a HubSpot Private App access token to the assistant's
Secret Manager as `HUBSPOT_PRIVATE_APP_TOKEN`. Optional metadata:
`HUBSPOT_PORTAL_ID` for HubSpot UI deep links.

All other behaviour is tunable via env vars on the assistant. Full table
in `guidance/sync_runbook.md`. The most common knobs:

| Env var | Default | Purpose |
| --- | --- | --- |
| `HUBSPOT_SYNC_MIN_INTERVAL_SECONDS` | `300` | Floor between sync ticks |
| `HUBSPOT_SYNC_OBJECT_INTERVALS` | `properties:86400,owners:3600,engagements:1800` | Per-object cadence overrides |
| `HUBSPOT_SYNC_OBJECTS` | (all CRM objects) | Which object types to sync |
| `HUBSPOT_SYNC_ENGAGEMENTS` | `false` | Engagements opt-in (high volume) |
| `HUBSPOT_API_PAGE_SIZE` | `100` | HubSpot search page size (max 100) |
| `HUBSPOT_ALLOW_BROADCAST_EMAIL` | `false` | Gates `broadcast_marketing_email` |
| `HUBSPOT_ALLOW_BROADCAST_SMS` | `false` | Gates `send_marketing_sms` |
| `HUBSPOT_ALLOW_CMS_PUBLISH` | `false` | Gates page/post publishing |
| `HUBSPOT_ALLOW_DELETE` | `false` | Gates `delete_*` functions |
| `HUBSPOT_CONFIG_JSON` | unset | One-shot JSON override for any of the above |

## Adopting the package

In a client's deployment:

```python
return BASE_SPEC.derive(
    name="v0",
    integrations=["hubspot"],
    guidance=[...]  # client-specific business context
)
```

That's it. The startup hook expands the `hubspot` slug into the package's
function/guidance assets via `expand_integrations`.

## Running the sync

Call `run_hubspot_sync_tick` on demand or from a client-owned schedule.
Cadence is gated by `HUBSPOT_SYNC_MIN_INTERVAL_SECONDS` plus per-object
overrides inside the orchestrator.

## High-stakes writes

Functions whose effects are visible to the customer's HubSpot users
require `confirm=True` AND a deployment-level config flag:

* `broadcast_marketing_email` (gated by `HUBSPOT_ALLOW_BROADCAST_EMAIL`)
* `send_marketing_sms` (gated by `HUBSPOT_ALLOW_BROADCAST_SMS`)
* `publish_cms_page`, `publish_blog_post` (gated by `HUBSPOT_ALLOW_CMS_PUBLISH`)
* `delete_*` functions (gated by `HUBSPOT_ALLOW_DELETE`)
* `send_quote`, `send_conversation_reply`

See `guidance/high_stakes_writes.md` for the full list and rationale.

## Tests

```bash
tests/parallel_run.sh tests/assistant_deployments/integrations
```

Symbolic AST checks (`test_function_compliance.py`) + manifest validation
cover the package automatically. Add live registration tests under
`tests/assistant_deployments/integrations/sync/` mirroring the github example.
A real-mode integration test gated on `HUBSPOT_PRIVATE_APP_TOKEN` being set
in the dev env exercises the live API; it skips in CI without the token.
