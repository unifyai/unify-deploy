# High-Stakes Writes

These functions have customer-visible side effects — confirm with the
user in plain English before passing `confirm=True`.  Many are also
gated behind a deployment-level config flag (operator opt-in).

| Function | Side effect | Config flag |
| --- | --- | --- |
| `broadcast_marketing_email` | Email goes to a recipient list | `HUBSPOT_ALLOW_BROADCAST_EMAIL` |
| `send_single_marketing_email` | Email goes to one recipient | (none — still confirm) |
| `send_marketing_sms` | SMS sent | `HUBSPOT_ALLOW_BROADCAST_SMS` |
| `send_quote` | Quote emailed to deal contacts | (none) |
| `send_conversation_reply` | Reply posts to inbox thread | (none) |
| `publish_cms_page` / `publish_blog_post` / `publish_hubdb_table` | Content goes live | `HUBSPOT_ALLOW_CMS_PUBLISH` |
| `delete_contact` / `delete_company` / `delete_deal` / `delete_list` / `delete_url_redirect` / `delete_cms_file` / `delete_hubdb_row` | Removes data | `HUBSPOT_ALLOW_DELETE` |

If the config flag is off, the function returns an error explaining how
to enable it — surface the message; don't enable on the user's behalf.
