# Engagements: Calls, Emails, Meetings, Notes, Tasks

HubSpot's "engagements" are records of activity.  All five types support
both read (`list_*`, `sync_*`) and write (`log_*` / `create_*` / `update_*`).

## Logging activity

The `associations` argument on every `log_*` and `create_*` function
attaches the engagement to one or more CRM records.  Pass it as
`[{"object_type": "contacts", "id": "12345"}, {"object_type": "deals", "id": "9001"}]`.

| Verb | Function | Use when |
| --- | --- | --- |
| `log_call` | `engagement_calls.log_call` | After a phone call |
| `log_email` | `engagement_emails.log_email` | After an email sent outside HubSpot (use `marketing_emails.send_*` for sends FROM HubSpot) |
| `log_meeting` | `engagement_meetings.log_meeting` | After a tour, owner discovery call, etc. |
| `create_note` | `engagement_notes.create_note` | Free-form context; "owner prefers monthly statements" |
| `create_task` | `engagement_tasks.create_task` | Future-dated to-do for a person |

## Tasks - lifecycle

Tasks have a status: `NOT_STARTED` → `IN_PROGRESS` → `COMPLETED`.  Use
`complete_task(task_id)` to mark done; use `update_task(task_id, {"hs_task_status": "IN_PROGRESS"})`
for in-progress.

## Why `log_email` is NOT for sending

`log_email` records that an email happened (subject, body, direction,
timestamps) and attaches it to CRM records.  It does not send an email.
For sending, see `marketing_emails.send_single_marketing_email` (single
recipient) or `broadcast_marketing_email` (recipient list, high-stakes).
