# Custom Fields — Long Format

Org-defined fields beyond EH's standard schema.  ClientZeta uses
custom fields for property-specific attributes: primary property, asset
class (BTL / HMO / Leasehold), specialist regions, etc.

## Storage shape

The sync uses **long format** to be robust against schema drift:

| Table | Shape |
|---|---|
| `EmploymentHero/CustomFields/Definitions` | `(id, name, data_type, options_json, is_required, applies_to)` — one row per field |
| `EmploymentHero/CustomFields/Values` | `(employee_id, field_id, value, updated_at)` — one row per (employee, field) pair |

`value` is stringified for portability; consult the definition for the
declared `data_type` to coerce on read.

## Querying

```python
# What custom fields does this org have?
await list_custom_field_definitions(mock=False)

# An employee's custom field values
await get_employee_custom_field_values("emp-1", mock=False)

# All values for one field across employees (e.g. who's at Battersea?)
await list_custom_field_values(field_id="cf-property", mock=False)
```

## Unpivoting in DataManager

For analytical queries that need a wide-format view (one column per
custom field), unpivot inside the query:

```sql
SELECT v.employee_id,
       MAX(CASE WHEN v.field_id = 'cf-property'    THEN v.value END) AS primary_property,
       MAX(CASE WHEN v.field_id = 'cf-asset-class' THEN v.value END) AS asset_class
FROM   EmploymentHero/CustomFields/Values v
GROUP BY v.employee_id;
```

Or join with the definitions table by name to make the query field-name
agnostic.
