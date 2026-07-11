"""HubSpot line_items sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_line_items(
    since: str | None = None,
    schema_version: str = "hubspot.crm.line_items.v1",
    mock: bool = True,
) -> dict:
    """Sync line items modified since ``since`` into a tables envelope."""
    if mock:
        from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_line_item,
        )

        base_props = {
            "name": "Property Management - Monthly Retainer",
            "quantity": "12",
            "price": "10000",
            "amount": "120000",
            "hs_product_id": "300",
            "createdate": "2026-04-10T08:00:00Z",
            "hs_lastmodifieddate": "2026-04-10T08:00:00Z",
        }
        rows = [
            normalize_line_item(
                {
                    "id": str(7000 + i),
                    "properties": {**base_props, "hs_object_id": str(7000 + i)},
                    "createdAt": "2026-04-10T08:00:00Z",
                    "updatedAt": "2026-04-10T08:00:00Z",
                    "archived": False,
                },
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"line_items": rows},
            "metadata": {
                "object_type": "line_items",
                "mode": "mock",
                "since": since,
                "row_count": len(rows),
            },
        }

    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_line_item,
    )

    default_props = [
        "name",
        "quantity",
        "price",
        "amount",
        "hs_product_id",
        "hs_object_id",
        "createdate",
        "hs_lastmodifieddate",
    ]
    cfg = get_hubspot_config()
    filter_groups = (
        [
            {
                "filters": [
                    {
                        "propertyName": "hs_lastmodifieddate",
                        "operator": "GT",
                        "value": since,
                    },
                ],
            },
        ]
        if since
        else []
    )
    rows, page_count, after = [], 0, None
    while True:
        body = await hubspot_search(
            "line_items",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=default_props,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"line_items": rows},
                "metadata": {
                    "object_type": "line_items",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(normalize_line_item(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (
            cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]
        ):
            break
    return {
        "schema_version": schema_version,
        "tables": {"line_items": rows},
        "metadata": {
            "object_type": "line_items",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
