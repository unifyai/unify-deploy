# High-Stakes Writes

Mutations require both:

1. **Deployment flag**: `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true`
2. **Call-site confirmation**: `confirm=True` argument

Without both, the function returns a structured refusal envelope with a
hint telling the user how to enable.

| Function | What it does |
|---|---|
| `submit_leave_request` | POST a new leave request |
| `submit_timesheet_entry` / `update_timesheet_entry` | Submit / amend a timesheet |
| `submit_expense_claim` | Submit a new expense claim |
| `acknowledge_policy` | Record a policy acknowledgement on the user's behalf |
| `create_employee_note` | Add a confidential HR note |
| `create_goal` / `update_goal_progress` | Create / progress a goal |
| `create_one_on_one` / `update_one_on_one_notes` | Schedule / annotate a 1:1 |
| `give_feedback` | Submit feedback on an employee |
| `give_cheer` / `give_hi_five` | Send recognition |

Assistant flow: restate the parameters in plain English, ask for
explicit confirmation in chat, then call with `confirm=True`.  If the
deployment flag is off, surface the refusal hint and suggest the user
ask their admin to enable.

When `EMPLOYMENTHERO_MIRROR_MUTATIONS_TO_DATAMANAGER=true` (default),
successful writes also update the synced DataManager same-tick so
subsequent `query_local_*` calls see the latest state without waiting
for the next sync.
