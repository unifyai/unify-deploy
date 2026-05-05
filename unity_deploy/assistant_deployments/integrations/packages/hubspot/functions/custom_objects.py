"""HubSpot Custom Objects - portal-specific object types.

Real estate firms typically model Properties, Units, Leases as custom
objects.  This module discovers schemas dynamically (no hardcoding of
type names), then exposes generic CRUD against any discovered type.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def discover_hubspot_custom_object_schemas(mock: bool = True) -> dict:
    """List all custom object schemas defined in this HubSpot portal.

    Call this BEFORE working with custom records - portal-specific
    fully-qualified names (e.g. ``p_<portalId>_property``) come from here.
    """
    if mock:
        return {
            "results": [
                {
                    "name": "property",
                    "fullyQualifiedName": "p_demo_property",
                    "labels": {"singular": "Property", "plural": "Properties"},
                    "primaryDisplayProperty": "name",
                    "secondaryDisplayProperties": ["address", "city", "state"],
                    "requiredProperties": ["name", "address"],
                    "searchableProperties": ["name", "address", "city"],
                    "associatedObjects": ["CONTACT", "COMPANY", "DEAL"],
                    "objectTypeId": "2-12345",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-04-01T00:00:00Z",
                    "archived": False,
                    "properties": [
                        {"name": "name", "type": "string", "label": "Property Name"},
                        {
                            "name": "address",
                            "type": "string",
                            "label": "Street Address",
                        },
                        {"name": "city", "type": "string", "label": "City"},
                        {"name": "state", "type": "string", "label": "State"},
                        {"name": "unit_count", "type": "number", "label": "Unit Count"},
                    ],
                },
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get("/crm/v3/schemas")
    if "error" in body:
        return body
    return {"results": body.get("results", [])}


@custom_function()
async def get_hubspot_custom_object_record(
    object_type: str,
    record_id: str,
    mock: bool = True,
) -> dict:
    """Fetch a single custom object record by ID.

    ``object_type`` is the fully-qualified name from
    ``discover_hubspot_custom_object_schemas``."""
    if mock:
        return {
            "id": str(record_id),
            "object_type": object_type,
            "properties": {
                "name": "Sunset Tower",
                "address": "100 Main St",
                "city": "Tampa",
                "state": "FL",
                "unit_count": "120",
            },
            "createdAt": "2026-04-01T10:00:00Z",
            "updatedAt": "2026-04-15T14:30:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/crm/v3/objects/{object_type}/{record_id}")


@custom_function()
async def search_hubspot_custom_objects(
    object_type: str,
    query: str,
    limit: int = 10,
    mock: bool = True,
) -> dict:
    """Search records of a custom object type."""
    if mock:
        return {
            "results": [
                {
                    "id": "11001",
                    "properties": {
                        "name": "Sunset Tower",
                        "address": "100 Main St",
                        "city": "Tampa",
                        "state": "FL",
                        "unit_count": "120",
                    },
                    "createdAt": "2026-04-01T10:00:00Z",
                    "updatedAt": "2026-04-15T14:30:00Z",
                    "archived": False,
                },
            ],
            "total": 1,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )

    return await hubspot_search(object_type, query=query, limit=min(limit, 100))


@custom_function()
async def list_hubspot_custom_objects(
    object_type: str,
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Paginate records of a custom object type."""
    if mock:
        base_props = {
            "name": "Sunset Tower",
            "address": "100 Main St",
            "city": "Tampa",
            "state": "FL",
            "unit_count": "120",
        }
        return {
            "results": [
                {
                    "id": str(11000 + i),
                    "properties": base_props,
                    "createdAt": "2026-04-01T10:00:00Z",
                    "updatedAt": "2026-04-15T14:30:00Z",
                    "archived": False,
                }
                for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get(f"/crm/v3/objects/{object_type}", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def create_hubspot_custom_object_record(
    object_type: str,
    properties: dict,
    mock: bool = True,
) -> dict:
    """Create a new custom-object record."""
    if mock:
        base_props = {
            "name": "New Record",
            "address": "Address",
            "city": "City",
            "state": "ST",
            "unit_count": "0",
        }
        return {
            "id": "99001",
            "properties": {**base_props, **properties},
            "createdAt": "2026-04-01T10:00:00Z",
            "updatedAt": "2026-04-15T14:30:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        f"/crm/v3/objects/{object_type}",
        {"properties": properties},
    )


@custom_function()
async def update_hubspot_custom_object_record(
    object_type: str,
    record_id: str,
    properties: dict,
    mock: bool = True,
) -> dict:
    """Patch properties on a custom-object record."""
    if mock:
        base_props = {
            "name": "Sunset Tower",
            "address": "100 Main St",
            "city": "Tampa",
            "state": "FL",
            "unit_count": "120",
        }
        return {
            "id": str(record_id),
            "properties": {**base_props, **properties},
            "createdAt": "2026-04-01T10:00:00Z",
            "updatedAt": "2026-04-15T14:30:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/{object_type}/{record_id}",
        {"properties": properties},
    )


@custom_function()
async def sync_hubspot_custom_objects(
    schema_version: str = "hubspot.crm.custom_objects.v1",
    mock: bool = True,
) -> dict:
    """Discover all custom object schemas and sync their records.

    Returns two tables: ``custom_object_schemas`` (one row per schema) and
    ``custom_object_records`` (one row per record across all types, with
    ``object_type`` discriminator)."""
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_custom_object_schema,
        normalize_object,
    )

    if mock:
        mock_schema = {
            "name": "property",
            "fullyQualifiedName": "p_demo_property",
            "labels": {"singular": "Property", "plural": "Properties"},
            "primaryDisplayProperty": "name",
            "secondaryDisplayProperties": ["address", "city", "state"],
            "requiredProperties": ["name", "address"],
            "searchableProperties": ["name", "address", "city"],
            "associatedObjects": ["CONTACT", "COMPANY", "DEAL"],
            "objectTypeId": "2-12345",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-04-01T00:00:00Z",
            "archived": False,
        }
        base_props = {
            "name": "Sunset Tower",
            "address": "100 Main St",
            "city": "Tampa",
            "state": "FL",
            "unit_count": "120",
        }
        schemas = [normalize_custom_object_schema(mock_schema)]
        records = [
            normalize_object(
                {
                    "id": str(11000 + i),
                    "properties": base_props,
                    "createdAt": "2026-04-01T10:00:00Z",
                    "updatedAt": "2026-04-15T14:30:00Z",
                    "archived": False,
                },
                object_type="p_demo_property",
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {
                "custom_object_schemas": schemas,
                "custom_object_records": records,
            },
            "metadata": {
                "object_type": "custom_objects",
                "mode": "mock",
                "schemas": len(schemas),
                "records": len(records),
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    cfg = get_hubspot_config()

    schemas_body = await hubspot_get("/crm/v3/schemas")
    if "error" in schemas_body:
        return {
            "schema_version": schema_version,
            "error": schemas_body["error"],
            "tables": {"custom_object_schemas": [], "custom_object_records": []},
            "metadata": {
                "object_type": "custom_objects",
                "mode": "real",
                "partial": True,
            },
        }

    raw_schemas = schemas_body.get("results", [])
    schemas = [normalize_custom_object_schema(s) for s in raw_schemas]
    records: list[dict] = []
    errors: list[dict] = []

    for s in raw_schemas:
        full_name = s.get("name") or s.get("fullyQualifiedName")
        if not full_name:
            continue
        after: str | None = None
        pages = 0
        while True:
            params: dict = {"limit": cfg["api_page_size"]}
            if after:
                params["after"] = after
            body = await hubspot_get(f"/crm/v3/objects/{full_name}", params=params)
            if "error" in body:
                errors.append({"object_type": full_name, "error": body["error"]})
                break
            for r in body.get("results", []):
                records.append(normalize_object(r, object_type=full_name))
            pages += 1
            after = body.get("paging", {}).get("next", {}).get("after")
            if not after or (
                cfg["max_pages_per_sync"] and pages >= cfg["max_pages_per_sync"]
            ):
                break

    return {
        "schema_version": schema_version,
        "tables": {
            "custom_object_schemas": schemas,
            "custom_object_records": records,
        },
        "metadata": {
            "object_type": "custom_objects",
            "mode": "real",
            "schemas": len(schemas),
            "records": len(records),
            "errors": errors,
            "partial": bool(errors),
        },
    }
