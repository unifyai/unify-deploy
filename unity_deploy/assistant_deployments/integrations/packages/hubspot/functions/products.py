"""HubSpot Products - product catalog CRUD + sync."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_hubspot_product(product_id: str, mock: bool = True) -> dict:
    """Fetch a single HubSpot product by ID."""
    if mock:
        return {
            "id": str(product_id),
            "properties": {
                "name": "Property Management - Monthly Retainer",
                "description": "Per-property monthly management fee.",
                "price": "10000",
                "hs_sku": "PMM-MONTHLY",
                "hs_product_type": "service",
                "hs_recurring_billing_period": "MONTHLY",
                "createdate": "2025-09-01T09:00:00Z",
                "hs_lastmodifieddate": "2026-01-15T11:00:00Z",
                "hs_object_id": str(product_id),
            },
            "createdAt": "2025-09-01T09:00:00Z",
            "updatedAt": "2026-01-15T11:00:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
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
    return await hubspot_get(
        f"/crm/v3/objects/products/{product_id}",
        params={"properties": ",".join(default_props)},
    )


@custom_function()
async def search_hubspot_products(query: str, limit: int = 10, mock: bool = True) -> dict:
    """Search HubSpot products."""
    if mock:
        return {
            "results": [
                {
                    "id": "300",
                    "properties": {
                        "name": "Property Management - Monthly Retainer",
                        "hs_sku": "PMM-MONTHLY",
                        "price": "10000",
                        "hs_object_id": "300",
                    },
                    "createdAt": "2025-09-01T09:00:00Z",
                    "updatedAt": "2026-01-15T11:00:00Z",
                    "archived": False,
                },
            ],
            "total": 1,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
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
    return await hubspot_search(
        "products",
        query=query,
        properties=default_props,
        limit=min(limit, 100),
    )


@custom_function()
async def list_hubspot_products(
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Paginate through HubSpot products."""
    if mock:
        base_props = {
            "name": "Property Management - Monthly Retainer",
            "description": "Per-property monthly management fee.",
            "price": "10000",
            "hs_sku": "PMM-MONTHLY",
            "hs_product_type": "service",
            "createdate": "2025-09-01T09:00:00Z",
            "hs_lastmodifieddate": "2026-01-15T11:00:00Z",
        }
        return {
            "results": [
                {
                    "id": str(300 + i),
                    "properties": {**base_props, "hs_object_id": str(300 + i)},
                    "createdAt": "2025-09-01T09:00:00Z",
                    "updatedAt": "2026-01-15T11:00:00Z",
                    "archived": False,
                }
                for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
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
    params: dict = {"limit": min(limit, 100), "properties": ",".join(default_props)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/products", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def create_hubspot_product(properties: dict, mock: bool = True) -> dict:
    """Create a HubSpot product."""
    if mock:
        base_props = {
            "name": "New Product",
            "price": "0",
            "hs_product_type": "service",
            "createdate": "2025-09-01T09:00:00Z",
            "hs_lastmodifieddate": "2025-09-01T09:00:00Z",
        }
        return {
            "id": "99001",
            "properties": {**base_props, "hs_object_id": "99001", **properties},
            "createdAt": "2025-09-01T09:00:00Z",
            "updatedAt": "2025-09-01T09:00:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post("/crm/v3/objects/products", {"properties": properties})


@custom_function()
async def update_hubspot_product(product_id: str, properties: dict, mock: bool = True) -> dict:
    """Patch HubSpot product properties."""
    if mock:
        base_props = {
            "name": "Property Management - Monthly Retainer",
            "price": "10000",
            "hs_sku": "PMM-MONTHLY",
        }
        return {
            "id": str(product_id),
            "properties": {**base_props, "hs_object_id": str(product_id), **properties},
            "createdAt": "2025-09-01T09:00:00Z",
            "updatedAt": "2026-01-15T11:00:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/products/{product_id}",
        {"properties": properties},
    )


@custom_function()
async def sync_hubspot_products(
    since: str | None = None,
    schema_version: str = "hubspot.crm.products.v1",
    mock: bool = True,
) -> dict:
    """Sync products modified since ``since`` into a tables envelope."""
    if mock:
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
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

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
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
