# Local DataManager Query vs. Live Employment Hero Call

For most reads, prefer `query_local_*` over the live API.  Reach for live
when freshness matters, when the field is sensitive (high-tier reviews,
critical-tier pay), or when the local copy hasn't synced yet.

| Question | Use |
|---|---|
| "How many active employees in the {Site B} team?" | `query_local_employees(team_id=...)` |
| "List operatives whose certificate expires in 30 days" | `query_local_expiring_qualifications(days_ahead=30)` |
| "What's Alex's current leave balance RIGHT NOW?" | `get_leave_balance(employee_id, mock=False)` — live |
| "Find all employees at the {Site B} location" | `query_local_employees(location_id=...)` |
| "Submit a leave request for Sam" | live `submit_leave_request` (writes always go live) |
| "What did Alex say in their Q1 self-assessment?" | live `get_review` — synced copy redacts free-text |
| "Show me Sam's payslip" | live `get_payslip` — payslips are never synced |

`query_local_*` returns a `freshness` block; if `is_fresh=False`, tell
the user the data may be stale and offer to refresh via the live
function.

If a local query returns nothing, check `get_sync_state` — the sync may
not have run for that object type yet.

Writes (`submit_*`, `acknowledge_*`, `create_*`, `update_*`,
`give_*`) always go live.  When
`EMPLOYMENTHERO_MIRROR_MUTATIONS_TO_DATAMANAGER=true` (default),
successful mutations also write back to DataManager same-tick.
