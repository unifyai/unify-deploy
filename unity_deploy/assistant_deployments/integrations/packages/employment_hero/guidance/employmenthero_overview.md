# Employment Hero Integration — Overview

Generic, reusable Employment Hero HRIS connector covering the full
public API.  The assistant uses this when:

- Reading or writing employee, leave, timesheet, expense, qualification,
  performance, recognition, survey, learning, recruitment, or pay data
  for the active organisation.
- Querying the synced DataManager copy for analytics that span many
  records (preferred over live API where freshness allows).
- Submitting writes (leave requests, timesheets, expenses, policy
  acknowledgements, goals, 1:1s, feedback, recognition).  Writes are
  gated by `confirm=True` AND a deployment-level config flag — see
  `employmenthero_high_stakes_writes.md`.

## Authentication

OAuth 2.0 (authorization code flow).  The customer creates a
developer-portal app at https://developer.employmenthero.com and pastes
its `EMPLOYMENTHERO_OAUTH_CLIENT_ID` + `EMPLOYMENTHERO_OAUTH_CLIENT_SECRET`
into Console -> Settings -> Secrets.  Clicking Connect in
Console -> Integrations runs the OAuth round-trip and writes
`EMPLOYMENTHERO_REFRESH_TOKEN`, `EMPLOYMENTHERO_ORGANISATION_ID`, and
`EMPLOYMENTHERO_HUB_DOMAIN` automatically.

The runtime mints short-lived access tokens from the refresh token
behind the scenes and caches them in-process for ~55 minutes.  The
dev-app's declared scopes determine which capabilities are usable;
capabilities the app doesn't cover surface as 403 envelopes via the
`tier_gating` pattern — never as exceptions.

## Two read paths

| Read path | When to use |
|---|---|
| `query_local_*` (DataManager) | Analytics, cross-record joins, "who across all properties has X expiring" |
| `get_*` / `list_*` / `search_*` (live API) | Fresh single-record fetches, "right now" queries, immediate post-write confirmations |

See `employmenthero_local_vs_live.md` for the decision rule.

## Three write tiers

1. **Standard write** — confirms with user, then mutates.  Examples:
   `submit_leave_request`, `submit_timesheet_entry`, `acknowledge_policy`.
   Requires `confirm=True` plus `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true`.
2. **High-tier read** — free-text fields are redacted at snapshot time;
   live API returns full content but the assistant follows
   `employmenthero_sensitive_data.md` refusal patterns.  Examples: performance reviews,
   1:1 notes, feedback, employee notes.
3. **Critical-tier read** — never synced; live-only with masked returns.
   Examples: payslips, banking, tax declarations, medical disclosures.
   Requires `confirm_user_authorised=True` and the assistant must
   explicitly confirm authority with the user before retrying.

## Sync mechanics

A single scenario per client (`<client>_eh_full_sync_v0.yaml`) drives
`run_employmenthero_sync_tick` on a 60-second scheduler tick.  The
orchestrator gates per-object cadence by env var so freshness is tunable
without redeploying the YAML.  See `employmenthero_sync_runbook.md`.
