# HubSpot Custom Objects

HubSpot lets each portal define its own object types in addition to the
standard Contacts/Companies/Deals/Tickets.  Real estate firms commonly
model `Property`, `Unit`, `Lease`; logistics firms model `Shipment`,
`Route`; etc.  Custom-object names are portal-specific and must be
discovered at runtime - never hardcode them.

## Always discover first

Before working with custom records, call:

```
schemas = await discover_custom_object_schemas(mock=False)
```

This returns the list of custom-object schemas in the connected portal,
each with:

- `name` / `fullyQualifiedName` - HubSpot's canonical type identifier
  (e.g. `p_<portalId>_property`).  Use this as the `object_type`
  argument to all custom-object functions.
- `labels` - human-readable singular + plural names.
- `primaryDisplayProperty` - the field to surface as the "name".
- `searchableProperties` - what `search_custom_objects` will search over.
- `associatedObjects` - which standard CRM objects can be linked
  (CONTACT, COMPANY, DEAL, TICKET).

The result is also available as a synced DataManager context at
`HubSpot/CRM/Dimensions/CustomObjectSchemas`.

## CRUD on custom records

- `get_custom_object_record(object_type, record_id)`
- `search_custom_objects(object_type, query)`
- `list_custom_objects(object_type, after=None, limit=25)`
- `create_custom_object_record(object_type, properties)`
- `update_custom_object_record(object_type, record_id, properties)`

## Associations to standard objects

Use `create_association` with the custom object type as either side:

```
await create_association(
    from_object_type="p_12345_property",
    from_id="11001",
    to_object_type="contacts",
    to_id="12345",
)
```

The exact `associationTypeId` depends on the portal's schema; the v4 API
allows `default` association which falls back to the HubSpot-defined
primary type.
