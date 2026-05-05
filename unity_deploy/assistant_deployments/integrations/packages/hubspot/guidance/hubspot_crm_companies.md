# HubSpot Company Records

How to look up and maintain organizations in HubSpot.

## Lookup

- **By domain** → `query_local_companies(domain=...)` first; HubSpot
  auto-deduplicates companies by domain so this is the most reliable key.
- **By name** → `search_companies` or `query_local_companies(name_query=...)`.

Surface: name, domain, industry, location, employee count, lifecycle stage,
type (PROSPECT/CUSTOMER/etc.).

## Create

`create_company({"name": ..., "domain": ..., "industry": ..., ...})`.  At
minimum supply `name` or `domain`.  HubSpot will deduplicate by domain.

## Associations

Most operations on a company involve linking it to contacts or deals.  Use
`create_association(from_object_type="contacts", from_id=..., to_object_type="companies", to_id=...)`
for the canonical primary association.

## Delete

`delete_company` (gated by `HUBSPOT_ALLOW_DELETE`).
