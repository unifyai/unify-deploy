# Correlating Matterport Views to HubSpot Leads

Goal: answer "which prospects engaged with which listings" by joining
Matterport view events to HubSpot contacts.

## How the join works

Primary key: **email**.

The Matterport Showcase URL accepts arbitrary query parameters that
are echoed onto every view event.  HubSpot landing pages can be
configured to append `?utm_email={{ contact.email }}` to the embed
URL.  The view event then carries that email back, and the package
joins it against `HubSpot/CRM/Dimensions/Contacts.email`.

The function: `correlate_matterport_views_to_hubspot_leads(model_id, since)`.

## Setup prerequisite for the customer

The customer needs to instrument their HubSpot CTAs:

> When you build a HubSpot landing page CTA that links to a Matterport
> tour, set the URL to:
>
> `https://my.matterport.com/show/?m=<model_id>&utm_email={{ contact.email }}`
>
> HubSpot replaces `{{ contact.email }}` with the logged-in contact's
> email automatically.

Without this, the join is a no-op — `correlate_*` returns an empty
matches array with the note "no events with utm_email."

## Privacy posture

- Only outcomes are stored.  We don't mirror Matterport's raw
  user-agent or IP.
- The `utm_email` is the contact's own email, surfaced via their own
  HubSpot interaction.  GDPR posture: the user is the data subject of
  their own engagement record.
- If the customer has an explicit "do not track" stance, they should
  not enable view-event sync (`MATTERPORT_SYNC_VIEW_EVENTS=false`)
  and the lead-correlation join is unavailable.

## Reading the matches

Each match has:

- `lead_email` — the email captured from `utm_email`
- `model_ids` — every model this email viewed (so a single contact
  who toured 3 listings shows once with 3 ids)
- `view_count` — total sessions across those models
- `last_viewed_at` — most recent session
- `hubspot_contact_id` — resolved from HubSpot contacts (null if the
  email isn't in HubSpot yet)
- `lifecyclestage` — HubSpot's pipeline stage for the contact

Sales hand-off prompt example: "Show me marketing-qualified leads who
viewed the Westwood listing this week" → call with
`model_id` set and `since` set to start of week, filter the result by
`lifecyclestage == "marketingqualifiedlead"`.

## When the join is partial

If a referrer arrives without `utm_email` (direct link, off-platform
share), the match falls through.  v1 doesn't attempt to resolve via
referrer URL → landing page → submission; that's a v2 enhancement once
volumes warrant it.
