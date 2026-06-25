# Tier Gating — 403 Handling

EH subscriptions and per-token scopes determine which API surfaces work.
When a function the token doesn't cover is called, EH returns 403; the
client surfaces this as a structured envelope (never raised).

For EH specifically, three things can cause a 403:
- the developer-portal app's declared **scope catalogue** doesn't
  include the endpoint;
- the connecting user's EH **role** doesn't permit it (admin/HR/payroll
  separation);
- the customer's EH **plan tier** doesn't include the module.

The hint in the envelope tells the user which resolution path applies.
Don't retry — the error is structural.

## What to surface

1. Tell the user which capability is unavailable, in plain English.
2. State the likely cause (token scope, role permissions, plan tier).
3. Offer the closest alternative the assistant can do at the current
   capability set.  Examples:
   - Pay endpoints unavailable -> offer to summarise leave or timesheet
     activity instead.
   - Recruitment unavailable -> offer to surface qualifications or
     onboarding status.
   - Performance unavailable -> goals (often available even when reviews
     aren't) or recognition.

## Capability probe

`probe_tier()` sweeps representative endpoints for each sync-object type
and returns `{object_key: {available: bool, status_code: int}}`.  Cached
in `EmploymentHero/Workforce/Meta/Capabilities` for
`EMPLOYMENTHERO_TIER_PROBE_TTL_SECONDS` (default 24h).

Operators can disable unavailable objects via `EMPLOYMENTHERO_SYNC_OBJECTS`
to reduce 403 noise in the sync audit log.

## Critical-tier endpoints are always 403-prone

`payslips`, `banking_and_tax`, and `medical` may 403 even on a
fully-scoped token if the calling user's role isn't payroll-admin or
HR-confidant.  This is **expected** — follow the refusal patterns in
`employmenthero_sensitive_data.md` rather than treating the 403 as an
error.
