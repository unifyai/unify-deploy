# Salesforce — sync

`run_salesforce_sync_tick` is the entrypoint the scenario runtime calls
on its interval. It reads per-object watermarks from
`Salesforce/Meta/SyncState`, dispatches each enabled object's `sync_*`
function, gates by per-object cadence, aggregates the returned tables,
and appends an audit row to `Salesforce/Meta/SyncRuns`.

## Watermarks

Each object syncs incrementally on `SystemModstamp`. The orchestrator
passes `since=<last_synced_at>` to each `sync_*` function; the function
issues
```sql
SELECT ... FROM <Object> WHERE SystemModstamp >= :since
ORDER BY SystemModstamp ASC LIMIT 200
```
Pagination follows `nextRecordsUrl` until exhausted or
`SALESFORCE_MAX_PAGES_PER_SYNC` is reached.

`full=true` ignores watermarks and pulls everything up to the page
cap — used for the initial bootstrap.

## Cadence

Per-object intervals (seconds) live in
`SALESFORCE_SYNC_OBJECT_INTERVALS`. Defaults:

| Object | Interval |
|---|---|
| `accounts` | 30 min |
| `contacts` | 30 min |
| `leads` | 15 min |
| `opportunities` | 15 min |
| `cases` | 15 min |

The scheduler may tick more often than any individual object's
cadence; the orchestrator skips objects whose watermark is too recent,
recording a `cadence_not_due` skip in the audit row. This decouples
scheduler frequency from real sync work, so operators can tune
freshness via env vars without redeploying the scenario.

## Disabling an object

Trim `SALESFORCE_SYNC_OBJECTS` to drop an object from the rotation.
Existing rows in DataManager are preserved; new deltas just stop
flowing.

## Audit + state contexts

| Context | Shape | Purpose |
|---|---|---|
| `Salesforce/Accounts` | `salesforce_accounts` rows | Mirror of Account |
| `Salesforce/Contacts` | `salesforce_contacts` rows | Mirror of Contact |
| `Salesforce/Leads` | `salesforce_leads` rows | Mirror of Lead |
| `Salesforce/Opportunities` | `salesforce_opportunities` rows | Mirror of Opportunity |
| `Salesforce/Cases` | `salesforce_cases` rows | Mirror of Case |
| `Salesforce/Meta/SyncState` | `{object_type, last_synced_at, last_status}` | Per-object watermarks |
| `Salesforce/Meta/SyncRuns` | append-only audit | Every tick: row totals, errors, skipped, config snapshot |

`get_salesforce_sync_state` returns the latest watermarks + the most
recent run.
