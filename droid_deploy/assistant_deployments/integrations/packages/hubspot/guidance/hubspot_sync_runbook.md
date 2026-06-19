# Sync Runbook

`run_hubspot_sync_tick` is the entrypoint the scenario runtime calls on
its interval.  It reads per-object watermarks from
`HubSpot/CRM/Meta/SyncState`, dispatches per-object sync functions whose
cadence has elapsed, aggregates returned tables, and appends a
`sync_runs` audit row.

## Cadence

- **Scheduler tick** (in scenario YAML): 60s default.
- **Per-object floor**: `HUBSPOT_SYNC_MIN_INTERVAL_SECONDS` (default 300)
  plus per-object overrides via `HUBSPOT_SYNC_OBJECT_INTERVALS`
  (default `properties:86400,owners:3600,engagements:1800`).

## Hyperparameters

Auth + portal:
- `HUBSPOT_PRIVATE_APP_TOKEN` (required), `HUBSPOT_PORTAL_ID`

Object selection:
- `HUBSPOT_SYNC_OBJECTS`, `HUBSPOT_SYNC_HUBS`
  (default `crm,engagements,marketing,sales,service`)
- `HUBSPOT_SYNC_CUSTOM_OBJECTS=true`
- `HUBSPOT_SYNC_ENGAGEMENTS=false` (high-volume opt-in),
  `HUBSPOT_SYNC_ENGAGEMENT_TYPES=calls,emails,meetings,notes,tasks`
- `HUBSPOT_SYNC_EMAIL_EVENTS=false`, `HUBSPOT_SYNC_AUDIT_LOGS=false`
  (extreme-volume opt-ins)

Property allowlists (default `all`):
- `HUBSPOT_CONTACT_PROPERTIES`, `HUBSPOT_COMPANY_PROPERTIES`,
  `HUBSPOT_DEAL_PROPERTIES`, `HUBSPOT_TICKET_PROPERTIES`

Behaviour:
- `HUBSPOT_API_PAGE_SIZE=100`, `HUBSPOT_MAX_PAGES_PER_SYNC` (unbounded)
- `HUBSPOT_REQUEST_TIMEOUT_SECONDS=30`,
  `HUBSPOT_RATE_LIMIT_MAX_RETRIES=3`,
  `HUBSPOT_RATE_LIMIT_BACKOFF_FACTOR=1.5`
- `HUBSPOT_LOCAL_FRESHNESS_THRESHOLD_SECONDS=3600`
- `HUBSPOT_MIRROR_MUTATIONS_TO_DATAMANAGER=true`
- `HUBSPOT_TIER_PROBE_TTL_SECONDS=86400`
- `HUBSPOT_CONFIG_JSON` — one-shot JSON override for any of the above

High-stakes gates: see `hubspot_high_stakes_writes.md`.

## Troubleshooting

- `get_sync_state` shows the last successful timestamp per object type.
- `sync_runs` table holds per-tick row counts and errors.
- 401/403 in `sync_runs.errors_json` means missing scopes — update the
  Private App in HubSpot, regenerate, replace the token in Secrets.
