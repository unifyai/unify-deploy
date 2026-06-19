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

## How to use

- **Live reads + writes** — call `employmenthero_request(method, path,
  params, body)` for any EH REST endpoint.  First-page only; bulk pulls
  go through the sync orchestrator.  4xx envelopes carry hints (e.g.
  tier-gated 403, missing scope) — read them.
- **Org-pinning** — most paths take an `{org_id}` segment.  Use
  `get_employmenthero_active_organisation` to resolve it; the helper
  prefers the pinned `EMPLOYMENTHERO_ORGANISATION_ID` and falls back
  to the first accessible org with a hint.
- **Bulk / snapshot** — `run_employmenthero_sync_tick(...)` mirrors
  workforce, leave, timesheets, expenses, policies, documents, custom
  fields, onboarding, qualifications, performance, recognition,
  surveys, learning, recruitment, and pay (rate-banded) into
  DataManager.
- **Local analytics** — once synced, prefer `query_local_employmenthero_*`
  helpers over re-hitting the API.

## Write safety — no code gate

`employmenthero_request` supports POST/PATCH/PUT/DELETE.  There is **no
code-level gate** — the actor applies the rules in
`employmenthero_high_stakes_writes.md` before issuing a destructive
verb.  In short: surface what's about to change, get explicit user
confirmation, then write.  Particularly sensitive surfaces:

- **Banking, super funds, tax declarations** — never written without
  explicit confirmation.
- **Pay runs, pay categories, employment terms** — financial impact;
  changes propagate to actual pay.
- **Employee personal data, medical disclosures, documents** —
  sensitive PII, often jurisdiction-regulated (UK / AU / NZ / SG).
- **Leave balances, timesheets** — entitlement and payroll impact.
- **Onboarding state, termination flows** — employment status.

## Sensitive reads — minimum disclosure

Reads can also leak PII into chat history.  Banking details, tax
declarations, payslip line-items, and medical disclosures should not
be returned verbatim into the actor's response unless the user has
explicitly authorised it.  For "is this person above pay band X?"
style questions, prefer the rate-banded snapshot in
`query_local_employmenthero_*` over a live exact-figure fetch.  See
`employmenthero_sensitive_data.md`.

## Sync

A scenario per client (`<client>_eh_full_sync_v0.yaml`) drives
`run_employmenthero_sync_tick` on a 60s tick.  Per-object cadence is
gated by env var so freshness is tunable without redeploying YAML.  See
`employmenthero_sync_runbook.md`.
