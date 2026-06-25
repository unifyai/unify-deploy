# Employment Hero Sync Runbook

`run_employmenthero_sync_tick` is the entrypoint the scenario runtime
calls on its interval.  It reads watermarks from
`EmploymentHero/Workforce/Meta/SyncState`, gates per-object by
`EMPLOYMENTHERO_SYNC_OBJECT_INTERVALS`, dispatches `sync_<object>`,
materialises returned tables into DataManager, and appends an audit row
to `SyncRuns`.

## Configuration knobs

All env vars; read fresh on every tick so changes apply without
redeploy.

| Env var | Default | Purpose |
|---|---|---|
| `EMPLOYMENTHERO_SYNC_MIN_INTERVAL_SECONDS` | `300` | Floor between sync ticks for any object |
| `EMPLOYMENTHERO_SYNC_OBJECT_INTERVALS` | (per-object defaults below) | Override cadence: `workforce:86400,leave:1800,...` |
| `EMPLOYMENTHERO_SYNC_OBJECTS` | (all 17) | Comma-separated allowlist; **exclude an object to disable it** |
| `EMPLOYMENTHERO_API_PAGE_SIZE` | `100` | Page size for paginated endpoints |
| `EMPLOYMENTHERO_MAX_PAGES_PER_SYNC` | unset | Cap pagination per object per tick |
| `EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS` / `_RATE_LIMIT_MAX_RETRIES` / `_RATE_LIMIT_BACKOFF_FACTOR` | `30` / `3` / `1.5` | HTTP behaviour |
| `EMPLOYMENTHERO_RECRUITMENT_RETENTION_DAYS` | `180` | Window for applicant sync |
| `EMPLOYMENTHERO_RECRUITMENT_REDACT_PII` | `true` | Strip applicant PII at snapshot time |
| `EMPLOYMENTHERO_RECRUITMENT_INCLUDE_REJECTED` | `false` | Include rejected applicants |
| `EMPLOYMENTHERO_REVIEWS_REDACT_FREE_TEXT` | `true` | Replace performance free-text with length+hash |
| `EMPLOYMENTHERO_EMPLOYEE_PERSONAL_REDACT` | `true` | Redact emergency-contact names/phones |
| `EMPLOYMENTHERO_PAY_RATE_BANDS` | `true` | Replace exact pay rates with coarse bands |
| `EMPLOYMENTHERO_SURVEYS_FORCE_ANONYMOUS` | `false` | Treat all surveys as anonymous regardless of EH flag |
| `EMPLOYMENTHERO_MIRROR_MUTATIONS_TO_DATAMANAGER` | `true` | Live writes also update DataManager same-tick |
| `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES` | `false` | Master gate for writes |
| `EMPLOYMENTHERO_TIER_PROBE_TTL_SECONDS` | `86400` | Capability probe cache lifetime |
| `EMPLOYMENTHERO_LOCAL_FRESHNESS_THRESHOLD_SECONDS` | unset (= 2× object interval) | When `query_local_*` flags data stale |
| `EMPLOYMENTHERO_CONFIG_JSON` | unset | One-shot JSON override for any of the above |

## Per-object cadence defaults

| Object | Cadence | Rationale |
|---|---|---|
| `workforce`, `employee_personal` | 24h | Profile changes are slow |
| `employee_notes` | 6h | HR notes are added throughout the day |
| `leave`, `expenses`, `recognition`, `recruitment` | 1h | Workday-active or event-driven |
| `timesheets` | 30m | High change rate during workday |
| `policies`, `custom_fields`, `performance`, `surveys`, `pay` | 24h | Slow / after pay-run cadence |
| `documents` | 12h | Mostly metadata |
| `onboarding`, `learning` | 6h | New-hire pipeline + training assignments |
| `qualifications` | 12h | Compliance gating — high-value but daily-ish |

## Operator playbook

**Initial bootstrap on a new deployment**:

1. Customer connects via Console -> Integrations -> Employment Hero
   (writes the OAuth secrets + auto-pinned org id).
2. `probe_employmenthero_tier(force=true)` — discover which capabilities
   the dev-app scopes cover.  Disable unavailable objects via
   `EMPLOYMENTHERO_SYNC_OBJECTS`.
3. `run_employmenthero_sync_tick(full=true, mock=false)` — manual
   bootstrap.
4. Verify `EmploymentHero/Workforce/Meta/SyncRuns` has an "ok" row.
5. Flip the scenario's `tasks[*].enabled` to `true`.

**Per-object cadence tuning**: watch `SyncRuns.errors_json` and
`skipped_json`.  High API churn -> increase the object's interval.
Stale-data complaints -> decrease it.  Rate limits -> lower
`EMPLOYMENTHERO_API_PAGE_SIZE` or raise `_RATE_LIMIT_MAX_RETRIES`.

**Disabling a capability**: remove from `EMPLOYMENTHERO_SYNC_OBJECTS`
(reversible without redeploy) or drop the capability id from the
deployment spec.

## Staging-smoke order before promotion

1. **FunctionManager registration smoke** — `unity_deploy.assistant_deployments.scenarios.cli`
   with `--no-materialize --no-outbox`.
2. **Scenario tick smoke** — same CLI without `--no-*`; inspect
   `primitives.data.list_tables(prefix="EmploymentHero/")`.
3. **Offline task activation smoke** — trigger the materialised
   `ScheduledTaskActivation` (e.g. via reconcile job's `run_now`);
   confirm the headless lane runs the tick and clears the activation.

Any failure blocks promotion.
