# HubSpot Deal Pipeline

How to inspect and progress deals.

## Lookup

1. **By name or description** → `search_deals(query=...)` or
   `query_local_deals(name_query=...)`.
2. **By stage / pipeline / owner** → `query_local_deals(stage=..., pipeline=...)`.
3. **By ID** → `get_deal(deal_id)`.

Surface: deal name, current stage, pipeline, amount, expected close date,
owner, priority, # associated contacts.

## Create

1. Required at create time: `dealname`, `pipeline`, ideally `dealstage`.
2. Other useful properties: `amount`, `closedate`, `dealtype` (newbusiness /
   existingbusiness), `description`, `hs_priority`, `hubspot_owner_id`.
3. Associate the deal with the contact + company via `create_association`
   immediately after creation.

## Stage transitions

`transition_deal_stage(deal_id, new_stage_id, note=...)` is preferred over
`update_deal({"dealstage": ...})` — the helper records a HubSpot note
explaining the transition when `note` is provided, leaving an audit trail.

For the customer's stage IDs, call `sync_pipelines` (or read the synced
`HubSpot/CRM/Dimensions/Pipelines` context) — pipeline + stage IDs are
portal-specific.

## Delete

`delete_deal` soft-deletes (HubSpot archives).  Gated by
`HUBSPOT_ALLOW_DELETE`.
