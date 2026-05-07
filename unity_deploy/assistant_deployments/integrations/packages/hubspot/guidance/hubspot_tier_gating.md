# HubSpot Tier Gating

HubSpot bills four Hubs (Marketing, Sales, Service, CMS) plus the free
CRM, each at multiple tiers (Free / Starter / Professional / Enterprise).
A bunch of API surfaces require specific tiers; calls against unsupported
tiers return 403, which the client surfaces as a structured envelope.

| Function | Tier required |
| --- | --- |
| `enroll_in_sequence` / `list_sequences` / `get_sequence` | Sales Hub Professional+ |
| `enroll_in_workflow` / `list_marketing_workflows` | Marketing Hub Professional+ |
| `send_marketing_sms` / `list_sms_messages` | Marketing Hub Pro+ with SMS add-on |
| `send_conversation_reply` | Service Hub Starter+ |
| `list_crm_reports` / `run_crm_report` | Reports add-on or Hub Pro+ |
| `list_marketing_emails` / `send_single_marketing_email` | Marketing Hub Starter+ |
| Custom Objects (`crm.objects.custom.*`) | Enterprise on most Hubs |

When a tier-gated call returns 403:

1. Tell the user which capability is unavailable.
2. Tell them which tier would unlock it.
3. Link to https://www.hubspot.com/products.
4. Offer the closest alternative their current tier supports.

Don't retry — the error is structural.

`_capabilities.probe_tier()` runs at startup (cached for
`HUBSPOT_TIER_PROBE_TTL_SECONDS`, default 24h) and stores the result at
`HubSpot/CRM/Meta/Capabilities`.
