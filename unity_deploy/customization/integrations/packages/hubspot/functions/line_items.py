"""HubSpot Line Items - per-deal product lines (CRUD + sync)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_DEFAULT_PROPERTIES = [
    "name", "quantity", "price", "amount", "hs_product_id",
    "hs_object_id", "createdate", "hs_lastmodifieddate",
]

_MOCK_LINE_ITEM = {
    "id": "7001",
    "properties": {
        "name": "Property Management - Monthly Retainer",
        "quantity": "12",
        "price": "10000",
        "amount": "120000",
        "hs_product_id": "300",
        "hs_object_id": "7001",
        "createdate": "2026-04-10T08:00:00Z",
        "hs_lastmodifieddate": "2026-04-10T08:00:00Z",
    },
    "createdAt": "2026-04-10T08:00:00Z",
    "updatedAt": "2026-04-10T08:00:00Z",
    "archived": False,
}


@custom_function()
async def get_line_item(line_item_id: str, mock: bool = True) -> dict:
    """Fetch a single line item."""
    if mock:
        return {**_MOCK_LINE_ITEM, "id": str(line_item_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(
        f"/crm/v3/objects/line_items/{line_item_id}",
        params={"properties": ",".join(_DEFAULT_PROPERTIES)},
    )


@custom_function()
async def list_line_items(after: str | None = None, limit: int = 25, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_LINE_ITEM, "id": str(7000 + i)} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100), "properties": ",".join(_DEFAULT_PROPERTIES)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/line_items", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def create_line_item(properties: dict, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_LINE_ITEM, "id": "99001",
                "properties": {**_MOCK_LINE_ITEM["properties"], **properties}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post("/crm/v3/objects/line_items", {"properties": properties})


@custom_function()
async def update_line_item(line_item_id: str, properties: dict, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_LINE_ITEM, "id": str(line_item_id),
                "properties": {**_MOCK_LINE_ITEM["properties"], **properties}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/line_items/{line_item_id}",
        {"properties": properties},
    )


@custom_function()
async def sync_line_items(
    since: str | None = None,
    schema_version: str = "hubspot.crm.line_items.v1",
    mock: bool = True,
) -> dict:
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_line_item,
        )
        rows = [normalize_line_item({**_MOCK_LINE_ITEM, "id": str(7000 + i)}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"line_items": rows},
            "metadata": {"object_type": "line_items", "mode": "mock",
                         "since": since, "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_line_item,
    )

    cfg = get_hubspot_config()
    filter_groups = (
        [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": since}]}]
        if since else []
    )
    rows, page_count, after = [], 0, None
    while True:
        body = await hubspot_search(
            "line_items",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=_DEFAULT_PROPERTIES,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"line_items": rows},
                    "metadata": {"object_type": "line_items", "mode": "real",
                                 "since": since, "row_count": len(rows),
                                 "pages": page_count, "partial": True}}
        rows.extend(normalize_line_item(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]):
            break
    return {
        "schema_version": schema_version,
        "tables": {"line_items": rows},
        "metadata": {"object_type": "line_items", "mode": "real",
                     "since": since, "row_count": len(rows), "pages": page_count},
    }
