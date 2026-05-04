# HubSpot Marketing Hub

Forms, campaigns, marketing emails, workflows, CTAs, subscriptions, and
events.  Several surfaces are tier-gated (Marketing Hub Pro+); see
`tier_gating.md`.

## Forms

- `list_marketing_forms`, `get_marketing_form` for definitions.
- `list_form_submissions` to read inquiries on a form.
- `submit_form` for programmatic submission (no auth - uses HubSpot's
  public submission endpoint with the portal_id + form_id).

## Marketing Emails

- `list_marketing_emails` / `get_marketing_email` to browse the email
  library.
- `send_single_marketing_email(email_id, contact_email, confirm=True)` for
  transactional one-off sends.
- `broadcast_marketing_email(email_id, list_id, confirm=True)` for sends to
  a recipient list.  HIGH-STAKES: also requires
  `HUBSPOT_ALLOW_BROADCAST_EMAIL=true`.  See `high_stakes_writes.md`.

## Workflows

`enroll_in_workflow(workflow_id, contact_email)` puts a contact into a
HubSpot workflow.  Workflow definitions are read-only via the API.

## Subscriptions

`list_subscription_types` returns the per-portal subscription set.
`subscribe_contact` / `unsubscribe_contact` change a contact's status for a
specific subscription type.  HubSpot requires a legal-basis string
(default `LEGITIMATE_INTEREST_PQL`) for subscribes - consult with the user
on which basis applies before defaulting.

## Email events

`sync_email_events` pulls per-recipient delivery / open / click / bounce
events.  HIGH VOLUME - opt-in via `HUBSPOT_SYNC_EMAIL_EVENTS=true`.

## SMS (tier-gated, HIGH-STAKES)

`send_marketing_sms` requires Marketing Hub Pro+ with the SMS add-on, both
`confirm=True` and `HUBSPOT_ALLOW_BROADCAST_SMS=true`.  See `high_stakes_writes.md`.
