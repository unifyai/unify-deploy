"""HubSpot custom_objects sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_custom_objects(
    schema_version: str = "hubspot.crm.custom_objects.v1",
    mock: bool = True,
) -> dict:
    """Discover all custom object schemas and sync their records.

    Returns two tables: ``custom_object_schemas`` (one row per schema) and
    ``custom_object_records`` (one row per record across all types, with
    ``object_type`` discriminator)."""
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
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

    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
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
