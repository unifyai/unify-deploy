# HubSpot Ticket Management

Service tickets - inquiries, complaints, maintenance requests, support.

## Lookup

- **By identifier** → `get_ticket(ticket_id)`.
- **By subject / content** → `query_local_tickets(subject_query=...)` first,
  then `search_tickets(query=..., mock=False)`.
- **Filter by stage / priority** → `query_local_tickets(pipeline_stage=..., priority=...)`.

Surface: subject, current pipeline stage, priority, source type, owner,
created date.

## Create

1. Required-ish: `subject`, `hs_pipeline_stage` (start state).
2. Useful: `content` (body), `hs_ticket_priority`, `source_type`,
   `hubspot_owner_id`.
3. Associate to the originating contact / company / deal with
   `create_association`.

## Stage transitions

Tickets have their own pipeline (separate from deals).  Update via
`update_ticket(ticket_id, {"hs_pipeline_stage": new_stage_id})`.  Confirm
with the user before moving to terminal states like `closed`.

## When NOT to use HubSpot tickets

If the customer has a dedicated PM platform (AppFolio, Yardi, Buildium) for
maintenance requests, that's the source of truth and HubSpot tickets are
unlikely to be primary.  Ask before creating tickets if it's unclear.
