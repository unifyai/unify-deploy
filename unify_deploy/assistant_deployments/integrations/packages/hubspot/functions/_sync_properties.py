"""HubSpot properties sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_properties(
    schema_version: str = "hubspot.crm.properties.v1",
    mock: bool = True,
) -> dict:
    """Sync property definitions across all canonical object types."""
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_property_def,
    )

    object_types = (
        "contacts",
        "companies",
        "deals",
        "tickets",
        "line_items",
        "products",
        "quotes",
    )

    if mock:
        rows = []
        for obj in object_types:
            rows.append(
                normalize_property_def(
                    {
                        "name": "firstname",
                        "label": "First Name",
                        "type": "string",
                        "fieldType": "text",
                        "groupName": "info",
                        "hubspotDefined": True,
                    },
                    object_type=obj,
                ),
            )
        return {
            "schema_version": schema_version,
            "tables": {"properties": rows},
            "metadata": {
                "object_type": "properties",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    rows: list[dict] = []
    errors: list[dict] = []
    for obj in object_types:
        body = await hubspot_get(f"/crm/v3/properties/{obj}")
        if "error" in body:
            errors.append({"object_type": obj, "error": body["error"]})
            continue
        rows.extend(
            normalize_property_def(p, object_type=obj) for p in body.get("results", [])
        )

    return {
        "schema_version": schema_version,
        "tables": {"properties": rows},
        "metadata": {
            "object_type": "properties",
            "mode": "real",
            "row_count": len(rows),
            "errors": errors,
            "partial": bool(errors),
        },
    }
