"""HubSpot Products - product catalog CRUD + sync."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_DEFAULT_PROPERTIES = [
    "name", "description", "price", "hs_sku", "hs_product_type",
    "hs_recurring_billing_period", "createdate", "hs_lastmodifieddate",
    "hs_object_id",
]

_MOCK_PRODUCT = {
    "id": "300",
    "properties": {
        "name": "Property Management - Monthly Retainer",
        "description": "Per-property monthly management fee.",
        "price": "10000",
        "hs_sku": "PMM-MONTHLY",
        "hs_product_type": "service",
        "hs_recurring_billing_period": "MONTHLY",
        "createdate": "2025-09-01T09:00:00Z",
        "hs_lastmodifieddate": "2026-01-15T11:00:00Z",
        "hs_object_id": "300",
    },
    "createdAt": "2025-09-01T09:00:00Z",
    "updatedAt": "2026-01-15T11:00:00Z",
    "archived": False,
}


@custom_function()
async def get_product(product_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_PRODUCT, "id": str(product_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(
        f"/crm/v3/objects/products/{product_id}",
        params={"properties": ",".join(_DEFAULT_PROPERTIES)},
    )


@custom_function()
async def search_products(query: str, limit: int = 10, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_PRODUCT, "id": "300"}], "total": 1}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )

    return await hubspot_search(
        "products",
        query=query,
        properties=_DEFAULT_PROPERTIES,
        limit=min(limit, 100),
    )


@custom_function()
async def list_products(after: str | None = None, limit: int = 25, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_PRODUCT, "id": str(300 + i)} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100), "properties": ",".join(_DEFAULT_PROPERTIES)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/products", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def create_product(properties: dict, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_PRODUCT, "id": "99001",
                "properties": {**_MOCK_PRODUCT["properties"], **properties}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post("/crm/v3/objects/products", {"properties": properties})


@custom_function()
async def update_product(product_id: str, properties: dict, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_PRODUCT, "id": str(product_id),
                "properties": {**_MOCK_PRODUCT["properties"], **properties}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/products/{product_id}",
        {"properties": properties},
    )


@custom_function()
async def sync_products(
    since: str | None = None,
    schema_version: str = "hubspot.crm.products.v1",
    mock: bool = True,
) -> dict:
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_product,
        )
        rows = [normalize_product({**_MOCK_PRODUCT, "id": str(300 + i)}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"products": rows},
            "metadata": {"object_type": "products", "mode": "mock",
                         "since": since, "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_product,
    )

    cfg = get_hubspot_config()
    filter_groups = (
        [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": since}]}]
        if since else []
    )
    rows, page_count, after = [], 0, None
    while True:
        body = await hubspot_search(
            "products",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=_DEFAULT_PROPERTIES,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"products": rows},
                    "metadata": {"object_type": "products", "mode": "real",
                                 "since": since, "row_count": len(rows),
                                 "pages": page_count, "partial": True}}
        rows.extend(normalize_product(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]):
            break
    return {
        "schema_version": schema_version,
        "tables": {"products": rows},
        "metadata": {"object_type": "products", "mode": "real",
                     "since": since, "row_count": len(rows), "pages": page_count},
    }
