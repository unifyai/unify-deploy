# Employment Hero Sync Runbook

How the sync works, what to tune, what to monitor.

## Architecture

```
[client]_eh_full_sync_v0.yaml       (per-client scenario YAML)
        |
        | tick on interval (60s scheduler tick)
        v
run_employmenthero_sync_tick(full=False)
        |
        | reads watermarks from EmploymentHero/Workforce/Meta/SyncState
        | gates per-object by EMPLOYMENTHERO_SYNC_OBJECT_INTERVALS
        | dispatches sync_<object>(since=<watermark>)
        |
        v
{schema_version, tables: {...}, metadata}
        |
        | scenario runtime materialises each table -> DataManager context
        v
EmploymentHero/{Employees, Leave/Requests, Qualifications/EmployeeRecords, ...}
        |
        | audit row appended to EmploymentHero/Workforce/Meta/SyncRuns
```

## Configuration knobs

All env vars; read fresh on every sync tick so changes take effect
without redeploy.

| Env var | Default | Purpose |
|---|---|---|
| `EMPLOYMENTHERO_SYNC_MIN_INTERVAL_SECONDS` | `300` | Floor between sync ticks for any object |
| `EMPLOYMENTHERO_SYNC_OBJECT_INTERVALS` | (per-object defaults below) | Override per-object cadence: `workforce:86400,leave:1800,...` |
| `EMPLOYMENTHERO_SYNC_OBJECTS` | (all 17) | Comma-separated allowlist; **exclude an object to disable it** |
| `EMPLOYMENTHERO_API_PAGE_SIZE` | `100` | Page size for paginated endpoints |
| `EMPLOYMENTHERO_MAX_PAGES_PER_SYNC` | unset | Cap pagination per object per tick (useful during initial backfill) |
| `EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout |
| `EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES` | `3` | 429 retry attempts |
| `EMPLOYMENTHERO_RATE_LIMIT_BACKOFF_FACTOR` | `1.5` | Exponential backoff base |
| `EMPLOYMENTHERO_RECRUITMENT_RETENTION_DAYS` | `180` | Window for applicant sync |
| `EMPLOYMENTHERO_RECRUITMENT_REDACT_PII` | `true` | Strip applicant PII at snapshot time |
| `EMPLOYMENTHERO_RECRUITMENT_INCLUDE_REJECTED` | `false` | Include rejected applicants in sync |
| `EMPLOYMENTHERO_REVIEWS_REDACT_FREE_TEXT` | `true` | Replace performance free-text with length+hash |
| `EMPLOYMENTHERO_EMPLOYEE_PERSONAL_REDACT` | `true` | Redact emergency-contact names/phones at snapshot |
| `EMPLOYMENTHERO_PAY_RATE_BANDS` | `true` | Replace exact pay rates with coarse bands at snapshot |
| `EMPLOYMENTHERO_SURVEYS_FORCE_ANONYMOUS` | `false` | Treat all surveys as anonymous regardless of EH flag |
| `EMPLOYMENTHERO_MIRROR_MUTATIONS_TO_DATAMANAGER` | `true` | Live writes also update DataManager same-tick |
| `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES` | `false` | Master gate for writes |
| `EMPLOYMENTHERO_TIER_PROBE_TTL_SECONDS` | `86400` | Capability probe cache lifetime |
| `EMPLOYMENTHERO_LOCAL_FRESHNESS_THRESHOLD_SECONDS` | unset (= 2× object interval) | When `query_local_*` flags data stale |
| `EMPLOYMENTHERO_CONFIG_JSON` | unset | One-shot JSON override for any of the above |

## Per-object cadence defaults

| Object | Default cadence | Rationale |
|---|---|---|
| `workforce` | 24h | Profile changes are slow |
| `employee_personal` | 24h | Personal details rarely change |
| `employee_notes` | 6h | HR notes are added throughout the day |
| `leave` | 1h | Workday-active |
| `timesheets` | 30m | High change rate during workday |
| `expenses` | 1h | Workday-active |
| `policies` | 24h | Policy versions rarely change |
| `documents` | 12h | Mostly metadata |
| `custom_fields` | 24h | Schema slow; values updated infrequently |
| `onboarding` | 6h | New hire pipeline; high churn at front-line |
| `qualifications` | 12h | Compliance gating — high-value but daily-ish |
| `performance` | 24h | Reviews/goals are slow |
| `recognition` | 1h | Event-driven |
| `surveys` | 24h | Slow |
| `learning` | 6h | Course assignments + completions |
| `recruitment` | 1h | Workday-active during hiring |
| `pay` | 24h | After pay-run cadence |

## Operator playbook

### Initial bootstrap (first tick on a new deployment)

1. Confirm `EMPLOYMENTHERO_ACCESS_TOKEN` is set in Console -> Secrets.
2. Confirm `EMPLOYMENTHERO_ORGANISATION_ID` is set if the token has
   access to multiple organisations.
3. Run `probe_tier(force=true)` to discover which capabilities the
   token covers.  Disable unavailable objects via
   `EMPLOYMENTHERO_SYNC_OBJECTS` to reduce 403 noise.
4. Trigger one sync manually with `run_employmenthero_sync_tick(full=true, mock=false)`.
5. Verify `EmploymentHero/Workforce/Meta/SyncRuns` has an "ok" row.
6. Flip the scenario's `tasks[*].enabled` to `true` so the schedule
   takes over.

### Per-object cadence tuning

Watch `EmploymentHero/Workforce/Meta/SyncRuns` — `errors_json` and
`skipped_json` columns surface anomalies.  Common adjustments:

- **High API churn** in `errors_json` → increase the object's interval.
- **User reports stale data** → decrease the relevant object's interval.
- **Rate limits hit** → bump `EMPLOYMENTHERO_API_PAGE_SIZE` lower or
  `EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES` higher.

### Disabling a capability

Two equivalent paths:

1. Remove from `EMPLOYMENTHERO_SYNC_OBJECTS` env-var list.
2. Edit the deployment spec's `integrations` to drop the relevant
   capability id.

The first is reversible without a redeploy; prefer it for ops tuning.

## Smoke order before promoting to staging/prod

Per the `unity-deploy` integrations README staging-smoke order:

1. **FunctionManager registration smoke** — invoke
   `unity_deploy.assistant_deployments.scenarios.cli` with `--no-materialize
   --no-outbox` to confirm the package's functions register.
2. **Scenario tick smoke** — same CLI without `--no-*` so the tick
   materialises tables through the real DataManager.  Inspect
   `primitives.data.list_tables(prefix="EmploymentHero/")` for expected
   contexts.
3. **Offline task activation smoke** — trigger the materialised
   `ScheduledTaskActivation` (e.g. via reconcile job's `run_now`) and
   confirm the headless lane wakes the activation, runs
   `run_employmenthero_sync_tick`, and clears the activation.

Any failure blocks promotion.
