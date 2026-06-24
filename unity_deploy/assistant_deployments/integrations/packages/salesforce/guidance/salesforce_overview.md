# Salesforce — overview

Generic Salesforce CRM connector. Standard objects (Accounts, Contacts,
Leads, Opportunities, Cases) have first-class read + sync functions;
custom objects, custom fields, and ad-hoc queries are reachable via
`run_salesforce_soql` and `describe_salesforce_object`.

## When to use what

- **`get_salesforce_*` / `list_salesforce_*`** — live read of one or a
  small handful of records. Hits the API every time. Use when freshness
  matters and the volume is small.
- **`sync_salesforce_*`** — snapshot a delta into DataManager. Returns
  the canonical `{tables, metadata}` envelope. Used by the sync
  orchestrator; rarely called directly by an actor.
- **`query_local_salesforce_*`** — query the DataManager mirror.
  Preferred for analytics: cheaper, faster, no rate-limit risk. Tradeoff
  is freshness — bounded by the per-object cadence in
  `SALESFORCE_SYNC_OBJECT_INTERVALS`.
- **`run_salesforce_soql`** — escape hatch. Custom objects, custom
  fields, joins, aggregations. Returns Salesforce's raw PascalCase keys
  (no flattening).
- **`describe_salesforce_object`** — discover field metadata before
  composing a SOQL query. Useful when the org has custom fields the
  agent doesn't know about ahead of time.

## Connection model

OAuth 2.0 web-server flow against `login.salesforce.com`. Sandbox
(`test.salesforce.com`) and customer My Domain logins are not supported
in v0 — every connection routes through the production login host.
After auth, REST traffic uses the per-org `instance_url` returned in
the token response.

The customer registers a Connected App in their Salesforce org, pastes
the Consumer Key + Consumer Secret into the Console Integrations modal,
clicks **Save and Connect**. The Console OAuth callback exchanges the
authorization code for a refresh token and persists both
`SALESFORCE_REFRESH_TOKEN` and `SALESFORCE_INSTANCE_URL`. See
`salesforce_setup.md`.

## Data shape

Sync functions return `{schema_version, tables: {salesforce_<object>:
[rows]}, metadata}`. Rows are flattened (no `attributes` envelope) and
keys are snake-cased — `LastModifiedDate` becomes `last_modified_date`.
Email fields are lower-cased so they join cleanly with HubSpot contacts
and Webex meeting invitees.

`run_salesforce_soql` is the exception: it returns Salesforce's raw
shape (PascalCase keys, `attributes` envelope present) so the actor can
work directly with field names from Salesforce describes.

## Common 403s

Salesforce permissions are layered: the Connected App's OAuth scopes,
the connecting user's profile / permission set, and per-record sharing.
A 403 on `sync_salesforce_opportunities` while `sync_salesforce_accounts`
works typically means the user's profile lacks Read on Opportunity, not
that the Connected App is misconfigured.

When in doubt, run `get_salesforce_me` first to confirm the connection,
then `describe_salesforce_object("Opportunity")` to confirm the user
can see the object's metadata.
