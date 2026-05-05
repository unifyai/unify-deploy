# Sync Runbook

How the periodic HubSpot → DataManager sync works, and how to tune it.

## What runs

The scenario `hubspot_crm_full_sync_v0` schedules `run_hubspot_sync_tick`
on an interval.  The tick:

1. Loads `HubSpot/CRM/Meta/SyncState` (one row per synced object type, with
   the last successful timestamp).
2. For each object type whose cadence has elapsed, calls the per-object
   sync function (e.g. `sync_contacts(since=last_synced_at)`).
3. Aggregates the per-object envelopes' `tables` into a single envelope.
4. The scenario runtime ingests each table per `data_targets`, upserting on
   the configured `unique_key`.
5. A `sync_runs` audit row is appended with started/finished timestamps,
   row counts per object, and any errors.

## Cadence

Two layers:

- **Scheduler tick rate** - `interval_seconds` in the scenario YAML
  (default `60s`).  The scenario runtime fires the tick this often.
- **Per-object cadence floor** - `HUBSPOT_SYNC_MIN_INTERVAL_SECONDS`
  (default `300s`) plus per-object overrides via
  `HUBSPOT_SYNC_OBJECT_INTERVALS`.  If less time has passed since the last
  successful sync of an object type than its cadence, the tick skips it.

Outcome: an operator can shorten cadence by changing the env var; no
scenario YAML edit needed.  The scheduler still ticks every 60s, but
actual API calls happen on the cadence the env var dictates.

## Hyperparameters

| Env var | Default | Purpose |
| --- | --- | --- |
| `HUBSPOT_PRIVATE_APP_TOKEN` | — | Auth bearer (no default; required for live mode) |
| `HUBSPOT_PORTAL_ID` | — | For HubSpot UI deep links |
| `HUBSPOT_SYNC_MIN_INTERVAL_SECONDS` | `300` | Floor between sync ticks |
| `HUBSPOT_SYNC_OBJECT_INTERVALS` | `properties:86400,owners:3600,engagements:1800` | Per-object cadence overrides |
| `HUBSPOT_SYNC_OBJECTS` | (all CRM objects) | Which CRM object types to sync |
| `HUBSPOT_SYNC_HUBS` | `crm,engagements,marketing,sales,service` | Hubs included in the orchestrator |
| `HUBSPOT_SYNC_CUSTOM_OBJECTS` | `true` | Discover + sync custom object types |
| `HUBSPOT_SYNC_ENGAGEMENTS` | `false` | High-volume; opt-in |
| `HUBSPOT_SYNC_ENGAGEMENT_TYPES` | `calls,emails,meetings,notes,tasks` | Which engagement types if enabled |
| `HUBSPOT_SYNC_EMAIL_EVENTS` | `false` | Opt-in - extreme volume |
| `HUBSPOT_SYNC_AUDIT_LOGS` | `false` | Opt-in - moderate volume |
| `HUBSPOT_API_PAGE_SIZE` | `100` | HubSpot search page size (max 100) |
| `HUBSPOT_MAX_PAGES_PER_SYNC` | unbounded | Safety cap per object per tick |
| `HUBSPOT_REQUEST_TIMEOUT_SECONDS` | `30` | httpx timeout |
| `HUBSPOT_RATE_LIMIT_MAX_RETRIES` | `3` | 429 retries |
| `HUBSPOT_RATE_LIMIT_BACKOFF_FACTOR` | `1.5` | Exponential backoff multiplier |
| `HUBSPOT_CONTACT_PROPERTIES` | `all` | Contact property allowlist (or `all`) |
| `HUBSPOT_COMPANY_PROPERTIES` | `all` | Company property allowlist |
| `HUBSPOT_DEAL_PROPERTIES` | `all` | Deal property allowlist |
| `HUBSPOT_TICKET_PROPERTIES` | `all` | Ticket property allowlist |
| `HUBSPOT_EMBED_ENABLED` | `true` | Embed text columns on ingest |
| `HUBSPOT_EMBED_STRATEGY` | `along` | DataManager embed mode |
| `HUBSPOT_LOCAL_FRESHNESS_THRESHOLD_SECONDS` | `3600` | When `query_local_*` flags data as stale |
| `HUBSPOT_MIRROR_MUTATIONS_TO_DATAMANAGER` | `true` | Mirror create/update back to DataManager |
| `HUBSPOT_ALLOW_BROADCAST_EMAIL` | `false` | Gates `broadcast_marketing_email` |
| `HUBSPOT_ALLOW_BROADCAST_SMS` | `false` | Gates `send_marketing_sms` |
| `HUBSPOT_ALLOW_CMS_PUBLISH` | `false` | Gates page/post/HubDB publish |
| `HUBSPOT_ALLOW_DELETE` | `false` | Gates `delete_*` |
| `HUBSPOT_TIER_PROBE_TTL_SECONDS` | `86400` | Capability probe cache TTL |
| `HUBSPOT_CONFIG_JSON` | unset | One-shot JSON override for any of the above |

## Tuning recipes

- **Fast-moving CRM** → drop `HUBSPOT_SYNC_MIN_INTERVAL_SECONDS` to 60-120s
  for the objects that change frequently.  Leave properties + owners alone.
- **Quota-constrained portal** → narrow `HUBSPOT_*_PROPERTIES` to canonical
  fields, lower `HUBSPOT_API_PAGE_SIZE` to 50, set `HUBSPOT_MAX_PAGES_PER_SYNC=5`.
- **Privacy-sensitive deployment** → `HUBSPOT_SYNC_ENGAGEMENTS=false`,
  `HUBSPOT_MIRROR_MUTATIONS_TO_DATAMANAGER=false`, all `HUBSPOT_ALLOW_*=false`.
- **High-volume engagements customer** → set
  `HUBSPOT_SYNC_OBJECT_INTERVALS=engagements:7200`, narrow types via
  `HUBSPOT_SYNC_ENGAGEMENT_TYPES`.

## Troubleshooting

- `get_sync_state` shows the last successful sync timestamp per object
  type.  If a type is missing, sync hasn't run for it yet.
- The `sync_runs` table holds the per-tick audit log with row counts and
  errors.  Filter the latest few rows when something looks off.
- 401/403 errors recorded in `sync_runs.errors_json` mean the Private App
  is missing scopes.  Update in HubSpot, regenerate the token, replace in
  Secrets.
