# Local DataManager Query vs. Live Employment Hero Call

The `employment_hero` package mirrors EH data into the assistant's
DataManager via `run_employmenthero_sync_tick`.  For most reads, prefer
the local copy.

## Decision rule

| Question | Use |
|---|---|
| "How many active employees in our Battersea team?" | `query_local_employees(team_id=...)` |
| "List operatives whose Gas Safe certificate expires in 30 days" | `query_local_expiring_qualifications(days_ahead=30)` |
| "What's Alex's current leave balance RIGHT NOW?" | `get_leave_balance(employee_id, mock=False)` — bypass local |
| "Find all employees at our Camden portfolio" | `query_local_employees(location_id=...)` |
| "Submit a leave request for Sam" | live `submit_leave_request` (writes always go live; mirror updates DataManager same-tick when `EMPLOYMENTHERO_MIRROR_MUTATIONS_TO_DATAMANAGER=true`) |
| "What did Alex say in their Q1 self-assessment?" | live `get_review` — synced copy redacts free-text |
| "Show me Sam's payslip" | live `get_payslip` — payslips are never synced |

## Freshness signal

Every `query_local_*` returns a `freshness` block:

```json
{
  "last_synced_at": "2026-04-30T02:15:00Z",
  "is_fresh": true,
  "threshold_seconds": 172800,
  "age_seconds": 3812
}
```

If `is_fresh` is `False`, the local copy is older than the threshold
(2× the configured per-object cadence by default).  Tell the user the
data may be slightly stale, and offer to refresh via the live function.

## When local query returns nothing

Two possibilities:

1. **The record genuinely doesn't exist** — confirm with a live
   `search_*`/`get_*` to be sure.  Possible if the customer just created
   it and the sync hasn't caught up.
2. **The sync hasn't run yet for this object type** — call
   `get_sync_state` to see the last successful tick.  If never, tell the
   user the integration is still bootstrapping and offer the live path.

## Writes are always live

`submit_leave_request`, `submit_timesheet_entry`, `submit_expense_claim`,
`acknowledge_policy`, `create_employee_note`, `create_goal`,
`update_goal_progress`, `create_one_on_one`, `update_one_on_one_notes`,
`give_feedback`, `give_cheer`, `give_hi_five` — all bypass the local
copy and go directly to Employment Hero.

When `EMPLOYMENTHERO_MIRROR_MUTATIONS_TO_DATAMANAGER=true` (default),
successful mutations also write back to DataManager so the local copy
stays consistent without waiting for the next sync tick.
