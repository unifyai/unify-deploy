# HubSpot Contact Management

How to look up, create, and update people in the customer's HubSpot CRM.

## Lookup

1. **By identifier (email, ID)** → `get_contact(contact_id)` if you have the
   numeric ID, or use `search_contacts(query=email)` for email lookup.
2. **By name** → call `query_local_contacts(name_query=...)` first; if the
   local copy is stale or returns nothing, fall through to
   `search_contacts(query=name, mock=False)`.
3. Surface: full name, email, phone, company, lifecycle stage, lead status.

## Create

1. Confirm at minimum the email address before calling `create_contact`.
2. The properties dict accepts any HubSpot contact property — `firstname`,
   `lastname`, `email`, `phone`, `company`, `jobtitle`, `lifecyclestage`,
   `hs_lead_status` are the canonical ones.
3. Echo back the new contact's ID and a HubSpot URL when `HUBSPOT_PORTAL_ID`
   is set: `https://app.hubspot.com/contacts/{portal_id}/contact/{id}`.

## Update

1. Send only the properties that changed via `update_contact(contact_id, properties)`.
2. For lifecycle stage transitions (Lead → MQL → SQL → Customer), confirm
   the new stage with the user before writing.

## Delete

`delete_contact` archives the contact (soft-delete in HubSpot).  Gated by
`HUBSPOT_ALLOW_DELETE` config flag.

## Errors

- 401/403 → the Private App is missing a contact-scope.  Tell the user
  exactly which scope (e.g. `crm.objects.contacts.write`), and to update
  the app in HubSpot, regenerate the token, replace it in Secrets.
- 429 → HubSpot rate-limited us.  The client backs off automatically.
