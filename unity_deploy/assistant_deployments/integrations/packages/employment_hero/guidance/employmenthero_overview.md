# Employment Hero Integration — Overview

Reusable HRIS connector covering employees, leave, timesheets, expenses,
qualifications, performance, recognition, surveys, learning,
recruitment, and pay surfaces of the Employment Hero public API.

## Authentication

OAuth 2.0 (authorization code flow).  The customer creates a
developer-portal app at https://developer.employmenthero.com and pastes
its `EMPLOYMENTHERO_OAUTH_CLIENT_ID` + `EMPLOYMENTHERO_OAUTH_CLIENT_SECRET`
into Console -> Settings -> Secrets.  Connecting in Console ->
Integrations runs the OAuth round-trip and writes
`EMPLOYMENTHERO_REFRESH_TOKEN`.  The active organisation is auto-pinned
to `EMPLOYMENTHERO_ORGANISATION_ID` when a single named org is found;
otherwise the operator picks via Settings -> Secrets.  See
`employmenthero_setup.md`.

## Three write tiers

1. **Standard write** — confirms with user, then mutates.  Examples:
   `submit_leave_request`, `submit_timesheet_entry`, `acknowledge_policy`.
   Requires `confirm=True` plus
   `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true`.
2. **High-tier read** — free-text fields are redacted at snapshot time;
   live API returns full content.  Examples: performance reviews, 1:1
   notes, feedback, employee notes.  Follow refusal patterns in
   `employmenthero_sensitive_data.md`.
3. **Critical-tier read** — never synced; live-only with masked returns.
   Examples: payslips, banking, tax declarations, medical disclosures.
   Requires `confirm_user_authorised=True` and explicit user
   confirmation.

## Sync

A scenario per client (`<client>_eh_full_sync_v0.yaml`) drives
`run_employmenthero_sync_tick` on a 60s tick.  Per-object cadence is
gated by env var so freshness is tunable without redeploying YAML.  See
`employmenthero_sync_runbook.md`.
