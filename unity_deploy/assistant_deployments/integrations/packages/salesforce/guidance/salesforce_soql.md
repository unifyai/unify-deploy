# Salesforce — SOQL escape hatch

`run_salesforce_soql` runs an arbitrary SOQL `SELECT` against the
connected org. It exists so the connector can serve every Salesforce
org's quirks — custom objects, custom fields, joins, aggregations —
without a code change per customer.

Read-only. The function rejects strings containing `INSERT`, `UPDATE`,
`DELETE`, `UPSERT`, or `MERGE` before they hit the wire (these are
Apex/DML, not SOQL — but easier-to-read errors than whatever Salesforce
returns).

## When to reach for it

- A field on a standard object isn't in the dedicated `list_*` /
  `get_*` shape (e.g. a `Description__c` custom field on Account).
- A custom object (`My_Object__c`) the connector doesn't know about.
- A join (`SELECT Account.Name, (SELECT Name FROM Contacts) FROM Account`).
- Aggregations (`SELECT COUNT(Id), StageName FROM Opportunity GROUP BY StageName`).

For repeat queries that would be cheaper as a sync, prefer adding the
field to the per-object module — that way the data lands in DataManager
and downstream queries don't pay the API cost every time.

## Discovering schema first

`describe_salesforce_object("Account")` returns the full Salesforce
describe payload — fields, types, picklist values, references. Useful
for validating field names before composing a SOQL query, or when the
agent encounters a custom field it doesn't recognise.

```python
desc = await describe_salesforce_object("My_Custom_Object__c")
field_names = [f["name"] for f in desc["fields"]]
```

## Output shape

`run_salesforce_soql` returns the canonical query envelope:
```json
{
  "records": [...],   // raw Salesforce shape — PascalCase, attributes envelope
  "totalSize": 1234,
  "done": true,       // false when truncated by SALESFORCE_MAX_PAGES_PER_SYNC
  "pages": 3
}
```

Records are *not* flattened — caller gets `Id`, `Name`, etc., not
`id`/`name`. This is deliberate: when working with custom fields the
PascalCase preserves the round-trip with describe responses.

## Pagination + safety

- `nextRecordsUrl` pagination is followed transparently.
- If `SALESFORCE_MAX_PAGES_PER_SYNC` is set and the query exceeds it,
  the response carries `done: false` and the partial records.
- Query timeouts and rate limits surface as the standard error
  envelope (`{error, status_code, body}`).

## Examples

```sql
-- Recent open opps over $50k
SELECT Id, Name, Amount, StageName
FROM Opportunity
WHERE IsClosed = false AND Amount > 50000
ORDER BY Amount DESC
LIMIT 50

-- Accounts with no opps in the last year
SELECT Id, Name FROM Account
WHERE Id NOT IN (
  SELECT AccountId FROM Opportunity
  WHERE CreatedDate > LAST_N_MONTHS:12
)
LIMIT 100

-- Pipeline by stage (aggregation)
SELECT StageName, COUNT(Id) cnt, SUM(Amount) total
FROM Opportunity
WHERE IsClosed = false
GROUP BY StageName
```
