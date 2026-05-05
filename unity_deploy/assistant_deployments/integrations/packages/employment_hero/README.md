# Employment Hero Integration

Generic, reusable Employment Hero HRIS connector covering the public API.
Lives under `integrations/packages/` so any client can opt in via
`integrations: ["employment_hero"]` on their deployment spec.

## What this package provides

* **On-demand functions** — get/list/search/create/update across employees,
  teams, locations, leave, timesheets, expenses, policies, documents,
  custom fields, onboarding, qualifications, performance management,
  recognition, surveys, learning, recruitment, and pay. Each function
  defaults to `mock=True` for safe testing; pass `mock=False` to hit live
  Employment Hero.
* **Sync functions** — pull deltas into the assistant's DataManager.
  Returns the canonical `{schema_version, tables, metadata}` envelope so
  the scenario runtime can route output via `data_targets`.
* **Local-query functions** — read the synced DataManager copy with a
  freshness signal. Preferred over live calls when freshness allows.
* **Sync orchestrator** — `run_employmenthero_sync_tick` aggregates all
  enabled object types into one envelope and gates per-object cadence by
  config.
* **Tier detection** — `probe_tier()` discovers which Employment Hero
  surfaces the token can access. Tier-gated functions return graceful
  error envelopes rather than raising on 403.
* **Critical-tier endpoints** — payslips, banking, tax declarations, and
  medical disclosures are live-only (never synced) and return masked
  values. The assistant must follow the refusal patterns in
  `sensitive_data.md`.
* **Guidance** — markdown files cover every domain plus cross-cutting
  concerns (sensitive data, tier gating, high-stakes writes, local vs
  live, sync runbook).

## Configuration

The customer adds an Employment Hero bearer token to the assistant's
Secret Manager as `EMPLOYMENTHERO_ACCESS_TOKEN`. Optional metadata:
`EMPLOYMENTHERO_ORGANISATION_ID` to pin the active organisation when the
token has access to multiple, and `EMPLOYMENTHERO_BASE_URL` to override
the API host (UK customers occasionally need
`https://api.employmenthero.co.uk`).

All other behaviour is tunable via env vars. Full table in
`guidance/sync_runbook.md`. The most common knobs:

| Env var | Default | Purpose |
| --- | --- | --- |
| `EMPLOYMENTHERO_SYNC_MIN_INTERVAL_SECONDS` | `300` | Floor between sync ticks |
| `EMPLOYMENTHERO_SYNC_OBJECT_INTERVALS` | (per-object defaults) | Per-object cadence overrides (`workforce:86400,leave:3600,...`) |
| `EMPLOYMENTHERO_SYNC_OBJECTS` | (all 17 objects) | Which object types to sync; exclude to disable |
| `EMPLOYMENTHERO_API_PAGE_SIZE` | `100` | Page size for list endpoints |
| `EMPLOYMENTHERO_RECRUITMENT_RETENTION_DAYS` | `180` | Only sync applicants modified in this window |
| `EMPLOYMENTHERO_RECRUITMENT_REDACT_PII` | `true` | Strip applicant PII at snapshot time |
| `EMPLOYMENTHERO_REVIEWS_REDACT_FREE_TEXT` | `true` | Replace performance free-text with length+hash |
| `EMPLOYMENTHERO_PAY_RATE_BANDS` | `true` | Band exact pay rates into ranges in snapshots |
| `EMPLOYMENTHERO_MIRROR_MUTATIONS_TO_DATAMANAGER` | `true` | Live writes also update DataManager same-tick |
| `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES` | `false` | Master gate for `submit_*` and `acknowledge_*` writes |
| `EMPLOYMENTHERO_TIER_PROBE_TTL_SECONDS` | `86400` | Cache TTL for `probe_tier()` |
| `EMPLOYMENTHERO_LOCAL_FRESHNESS_THRESHOLD_SECONDS` | (per-object 2× interval) | When to flag a local query as stale |
| `EMPLOYMENTHERO_CONFIG_JSON` | unset | One-shot JSON override for any of the above |

## Adopting the package

In a client's deployment:

```python
return BASE_SPEC.derive(
    name="default",
    integrations=["employment_hero"],
)
```

That's it. The startup hook expands the `employment_hero` slug into the
package's function/guidance assets via `expand_integrations`. To also
schedule the incremental sync, add a per-client sync package (e.g.
`clientzeta_eh_sync`) that defines the scenario YAML with the client's
assistant id pinned in `tasks[*].target.assistant_id`.

## Running the sync

A client's `<client>_eh_sync` package ships
`<client>_eh_full_sync_v0.yaml`, scheduling `run_employmenthero_sync_tick`
on a low-overhead 60-second scheduler tick. The actual sync cadence is
gated by `EMPLOYMENTHERO_SYNC_MIN_INTERVAL_SECONDS` plus per-object
overrides inside the orchestrator, so operators can tune freshness via
env-var without redeploying the YAML.

The scenario's `tasks[*].enabled` ships `false`. Operators flip it to
`true` after the control-plane has the TaskScheduler + FunctionManager
ids seeded for the client (per the `a71a840` convention).

## High-stakes writes

`submit_leave_request`, `submit_timesheet_entry`, `submit_expense_claim`,
`acknowledge_policy`, `create_employee_note`, `give_cheer`, `give_hi_five`,
`give_feedback`, `create_goal`, `update_goal_progress`, `create_one_on_one`,
and `update_one_on_one_notes` require `confirm=True` AND
`EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true` to mutate live data.
Without those, they return a structured refusal envelope so the assistant
can ask the user to flip the gate explicitly.

## Critical-tier handling

Payslips, banking, tax declarations, and medical disclosures are
**live-only** — never synced into DataManager — and return **masked**
values (e.g. `****1234` for account numbers). The full content must be
viewed in Employment Hero directly. See `guidance/sensitive_data.md` and
`guidance/pay_and_banking.md` for the refusal/redirect patterns the
assistant follows.

## Migration story

The integration runs in two phases that **coexist indefinitely**:

1. **Live reads only** (Phase 1): assistant calls EH API on every request.
2. **Live reads + scheduled DataManager sync** (Phase 2 — the standard
   end state): periodic syncs materialise EH data into DataManager
   contexts under `EmploymentHero/...`. The assistant uses
   `query_local_*` for analytical queries and live functions for fresh
   single-record fetches and writes.

A separate "decommission live calls" milestone is **not** assumed. After
12-18 months of stable Phase-2 operation, the team reviews usage data to
decide whether any live read functions can be retired.
