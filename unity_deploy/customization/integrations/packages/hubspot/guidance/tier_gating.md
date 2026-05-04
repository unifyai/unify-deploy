# HubSpot Tier Gating

HubSpot bills its product across four Hubs (Marketing, Sales, Service, CMS)
plus the free CRM, each at multiple tiers (Free / Starter / Professional /
Enterprise).  A bunch of API surfaces require specific tiers.

## How the package handles it

When a tier-gated function is called and the customer's portal doesn't have
the right subscription, HubSpot returns a 403.  The internal HTTP client
recognises this and returns an error envelope:

```json
{
  "error": "HubSpot GET /automation/v4/sequences returned 403",
  "status_code": 403,
  "hint": "403 typically indicates a missing scope on the Private App or a HubSpot tier that does not include this surface."
}
```

## Which surfaces are tier-gated

| Function | Tier required |
| --- | --- |
| `enroll_in_sequence` / `list_sequences` / `get_sequence` | Sales Hub Professional+ |
| `enroll_in_workflow` / `list_marketing_workflows` | Marketing Hub Professional+ |
| `send_marketing_sms` / `list_sms_messages` | Marketing Hub Pro+ with SMS add-on |
| `send_conversation_reply` (live chat / inbox) | Service Hub Starter+ |
| `list_crm_reports` / `run_crm_report` | Reports add-on or Hub Pro+ |
| `list_marketing_emails` / `send_single_marketing_email` | Marketing Hub Starter+ |
| Custom Objects (`crm.objects.custom.*`) | Enterprise on most Hubs |

## What to surface to the user

1. Tell them which capability is unavailable, in plain English.
2. Tell them which tier would unlock it.
3. Link them to https://www.hubspot.com/products to upgrade.
4. Offer the closest alternative we can do at their current tier.

Don't retry tier-gated calls.  The error is structural, not transient.

## Capability probe

`_capabilities.probe_tier()` runs a sweep at startup (cached for
`HUBSPOT_TIER_PROBE_TTL_SECONDS`, default 24 hours) to identify which
surfaces are available.  The probe result is stored at
`HubSpot/CRM/Meta/Capabilities`.
