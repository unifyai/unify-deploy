"""HubSpot products sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_products(
    since: str | None = None,
    schema_version: str = "hubspot.crm.products.v1",
    mock: bool = True,
) -> dict:
    """Sync products modified since ``since`` into a tables envelope."""
    if mock:
        from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_product,
        )

        base_props = {
            "name": "Property Management - Monthly Retainer",
            "description": "Per-property monthly management fee.",
            "price": "10000",
            "hs_sku": "PMM-MONTHLY",
            "hs_product_type": "service",
            "createdate": "2025-09-01T09:00:00Z",
            "hs_lastmodifieddate": "2026-01-15T11:00:00Z",
        }
        rows = [
            normalize_product(
                {
                    "id": str(300 + i),
                    "properties": {**base_props, "hs_object_id": str(300 + i)},
                    "createdAt": "2025-09-01T09:00:00Z",
                    "updatedAt": "2026-01-15T11:00:00Z",
                    "archived": False,
                },
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"products": rows},
            "metadata": {
                "object_type": "products",
                "mode": "mock",
                "since": since,
                "row_count": len(rows),
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_product,
    )

    default_props = [
        "name",
        "description",
        "price",
        "hs_sku",
        "hs_product_type",
        "hs_recurring_billing_period",
        "createdate",
        "hs_lastmodifieddate",
        "hs_object_id",
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
            "products",
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
                "tables": {"products": rows},
                "metadata": {
                    "object_type": "products",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(normalize_product(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (
            cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]
        ):
            break
    return {
        "schema_version": schema_version,
        "tables": {"products": rows},
        "metadata": {
            "object_type": "products",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
