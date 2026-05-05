# Tier Gating — 403 Handling

Employment Hero subscriptions and per-token scopes determine which API
surfaces work.  When a function the user's token doesn't cover is
called, EH returns 403.  The package's HTTP client recognises this and
returns a structured envelope:

```json
{
  "error": "Employment Hero GET /api/v1/organisations/.../pay_runs returned 403",
  "status_code": 403,
  "hint": "403 typically indicates the user's EMPLOYMENTHERO_ACCESS_TOKEN lacks the scope for this endpoint, or the user's role in EH doesn't have permission for it. Tell the user which capability is unavailable and suggest they regenerate the token with the needed scope, or contact their EH admin."
}
```

## What to surface to the user

1. Tell them which capability is unavailable in plain English.
2. State the likely cause (token scope, role permissions, EH plan tier).
3. Offer the closest alternative the assistant can do at their current
   tier.  Examples:
   - Pay endpoints unavailable → offer to summarise leave or timesheet
     activity instead.
   - Recruitment unavailable → offer to surface qualifications or
     onboarding status.
   - Performance unavailable → offer to surface goals (often available
     even when reviews aren't) or recognition.

Don't retry tier-gated calls.  The error is structural, not transient.

## Capability probe

`probe_tier()` sweeps representative endpoints for each sync-object
type and returns `{object_key: {available: bool, status_code: int}}`.
The result is cached in `EmploymentHero/Workforce/Meta/Capabilities`
for `EMPLOYMENTHERO_TIER_PROBE_TTL_SECONDS` (default 24h).

Operators can disable unavailable objects via
`EMPLOYMENTHERO_SYNC_OBJECTS` to reduce 403 noise in the sync audit log.

## Critical-tier endpoints are always 403-prone

`payslips`, `banking_and_tax`, and `medical` may 403 even on a
fully-scoped token if the calling user's role isn't payroll-admin or
HR-confidant.  This is **expected** — the assistant should follow the
refusal patterns in `employmenthero_sensitive_data.md` rather than treating the 403 as
an error.
