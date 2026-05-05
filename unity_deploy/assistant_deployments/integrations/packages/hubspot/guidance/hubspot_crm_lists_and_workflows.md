# Lists vs. Workflows

HubSpot has two related-but-different segmentation primitives.  Pick
intentionally.

## Lists

A **list** is a collection of contacts.

- **Static lists** are manually managed - you `add_contact_to_list` /
  `remove_contact_from_list` to change membership.
- **Dynamic lists** auto-populate based on filter rules defined in HubSpot's
  UI; membership is read-only via the API.  Don't try to add contacts to a
  dynamic list - HubSpot will reject it.

Use lists when the customer says "show me everyone in segment X" or "send
this email to my Q3 prospects" or "who hasn't replied in the past 30 days".

## Workflows

A **workflow** is a sequence of automated actions triggered by enrollment.

- `enroll_in_workflow(workflow_id, contact_email)` puts a contact into a
  pre-defined workflow; the workflow then runs its actions (send email,
  set property, create task, etc.) on a schedule.
- `unenroll_from_workflow` removes a contact from a workflow's enrollment.

Use workflows when the customer says "kick off the new-tenant nurture for
this contact" or "stop the owner-outreach drip on this account".

## Common confusion

- **Adding a contact to a list ≠ enrolling them in a workflow.**  These are
  separate operations even when the workflow's enrollment trigger is "joined
  list X".  HubSpot's enrollment trigger watches the list and enrolls the
  contact for you, but only if the workflow is set up that way.
- **Workflow enrollments are tier-gated** (Marketing Hub Pro+); list
  membership is not.  See `tier_gating.md`.
