# High-Stakes Writes

Some HubSpot operations have customer-visible side effects: an email gets
sent, a page goes live, a record is destroyed, an SMS is delivered.  These
functions all require `confirm=True` from the caller, and many are
additionally gated behind a deployment-level config flag.

## The list

| Function | Side effect | Caller `confirm=True` | Config flag |
| --- | --- | --- | --- |
| `broadcast_marketing_email` | Email goes to a recipient list | Required | `HUBSPOT_ALLOW_BROADCAST_EMAIL=true` |
| `send_single_marketing_email` | Email goes to one recipient | Required | (none, but should still confirm) |
| `send_marketing_sms` | SMS sent | Required | `HUBSPOT_ALLOW_BROADCAST_SMS=true` |
| `send_quote` | Quote emailed to deal contacts | Required | (none) |
| `send_conversation_reply` | Reply posts to inbox thread | Required | (none) |
| `publish_cms_page` | Public web page goes live | Required | `HUBSPOT_ALLOW_CMS_PUBLISH=true` |
| `publish_blog_post` | Public blog post goes live | Required | `HUBSPOT_ALLOW_CMS_PUBLISH=true` |
| `publish_hubdb_table` | HubDB changes live | Required | `HUBSPOT_ALLOW_CMS_PUBLISH=true` |
| `delete_contact` / `delete_company` / `delete_deal` / `delete_list` / `delete_url_redirect` / `delete_cms_file` / `delete_hubdb_row` | Removes data | (none, but config-gated) | `HUBSPOT_ALLOW_DELETE=true` |

## How to handle these

1. **Always confirm with the user before passing `confirm=True`.**  State
   plainly what will happen (e.g. "I'm about to send the Q3 newsletter to
   1,250 contacts on the 'All Owners' list - confirm?").
2. **If the config flag is off, the function returns an error explaining
   how to enable it.**  Surface the message; offer the user the option to
   enable, but don't enable on their behalf.
3. **Walk back if the user changes their mind.**  Several of these
   operations are reversible (drafts can be edited; pages can be
   unpublished).  Some are not (a sent email cannot be unsent).

## Why two layers

- `confirm=True` is per-call - the LLM must explicitly opt in for each
  operation.  This catches accidental invocations.
- The config flag is per-deployment - the operator sets it once, scoped to
  the assistant.  This catches accidental enablement of broadcast
  capabilities entirely.

Both protections must be passed for the operation to proceed.
