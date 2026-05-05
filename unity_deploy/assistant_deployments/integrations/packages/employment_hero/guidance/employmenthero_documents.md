# Documents

**Metadata-only sync.**  File contents are not downloaded by the
package — the synced `EmploymentHero/Documents` table contains the
record id, type, name, signed-status, and (potentially short-lived) URL.

## Common queries

```python
# Documents on file for a specific employee
await query_local_documents(employee_id="emp-1", mock=False)

# All Right-To-Work documents across the org
await query_local_documents(document_type="right_to_work", mock=False)

# Live metadata fetch (refreshes URL signing)
await get_document_metadata("doc-1", mock=False)
```

## Document templates

```python
await list_document_templates(mock=False)
```

Templates are the org's reusable document forms (employment contract
templates, NDA templates, role-specific addendums).

## Reading file contents

The package does **not** fetch document file contents.  If the user
needs the actual document, the assistant should:

1. Confirm the document exists via `get_document_metadata`.
2. Tell the user the EH-side URL is available on the metadata
   record.
3. Recommend the user download / view the document in Employment Hero
   directly — the URL on the metadata may be short-lived and isn't
   safe to relay.
