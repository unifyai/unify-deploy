# High-Stakes Writes

Functions that mutate live Employment Hero data require **two** gates:

1. **Deployment-level config flag**: `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true`.
2. **Call-site confirmation**: `confirm=True` argument on the function.

Without both, the function returns a structured refusal envelope:

```json
{
  "error": "Refused: EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES is not true.",
  "hint": "..."
}
```

## Gated functions

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

## Assistant flow

1. The user asks for a write.
2. The assistant restates the parameters in plain English (employee,
   amounts, dates).
3. The assistant asks the user to confirm explicitly in the chat.
4. After an explicit yes, the assistant calls the function with
   `confirm=True`.
5. If the deployment flag isn't set, the function refuses with a
   structured hint.  The assistant relays the hint to the user and
   suggests they ask their admin to enable
   `EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES=true`.

## Why two gates

- The deployment flag is the **operator-set rail** — it answers
  "should this assistant ever be allowed to mutate EH data?"
- `confirm=True` is the **per-call rail** — it answers "did the
  assistant actually verify with the user before this specific call?"

Both must align before a mutation goes through.

## Mutation mirroring

When `EMPLOYMENTHERO_MIRROR_MUTATIONS_TO_DATAMANAGER=true` (default),
successful writes also update the corresponding DataManager context
same-tick so subsequent `query_local_*` calls see the latest state
without waiting for the next sync.
