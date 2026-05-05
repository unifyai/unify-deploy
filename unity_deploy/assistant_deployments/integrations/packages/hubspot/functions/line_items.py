"""HubSpot Line Items - per-deal product lines (CRUD + sync)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_hubspot_line_item(line_item_id: str, mock: bool = True) -> dict:
    """Fetch a single line item."""
    if mock:
        return {
            "id": str(line_item_id),
            "properties": {
                "name": "Property Management - Monthly Retainer",
                "quantity": "12",
                "price": "10000",
                "amount": "120000",
                "hs_product_id": "300",
                "hs_object_id": str(line_item_id),
                "createdate": "2026-04-10T08:00:00Z",
                "hs_lastmodifieddate": "2026-04-10T08:00:00Z",
            },
            "createdAt": "2026-04-10T08:00:00Z",
            "updatedAt": "2026-04-10T08:00:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
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
    return await hubspot_get(
        f"/crm/v3/objects/line_items/{line_item_id}",
        params={"properties": ",".join(default_props)},
    )


@custom_function()
async def list_hubspot_line_items(
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Paginate through HubSpot line items."""
    if mock:
        base_props = {
            "name": "Property Management - Monthly Retainer",
            "quantity": "12",
            "price": "10000",
            "amount": "120000",
            "hs_product_id": "300",
            "createdate": "2026-04-10T08:00:00Z",
            "hs_lastmodifieddate": "2026-04-10T08:00:00Z",
        }
        return {
            "results": [
                {
                    "id": str(7000 + i),
                    "properties": {**base_props, "hs_object_id": str(7000 + i)},
                    "createdAt": "2026-04-10T08:00:00Z",
                    "updatedAt": "2026-04-10T08:00:00Z",
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
        "quantity",
        "price",
        "amount",
        "hs_product_id",
        "hs_object_id",
        "createdate",
        "hs_lastmodifieddate",
    ]
    params: dict = {"limit": min(limit, 100), "properties": ",".join(default_props)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/line_items", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def create_hubspot_line_item(properties: dict, mock: bool = True) -> dict:
    """Create a HubSpot line item."""
    if mock:
        base_props = {
            "name": "Line Item",
            "quantity": "1",
            "price": "0",
            "amount": "0",
            "createdate": "2026-04-10T08:00:00Z",
            "hs_lastmodifieddate": "2026-04-10T08:00:00Z",
        }
        return {
            "id": "99001",
            "properties": {**base_props, "hs_object_id": "99001", **properties},
            "createdAt": "2026-04-10T08:00:00Z",
            "updatedAt": "2026-04-10T08:00:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post("/crm/v3/objects/line_items", {"properties": properties})


@custom_function()
async def update_hubspot_line_item(
    line_item_id: str,
    properties: dict,
    mock: bool = True,
) -> dict:
    """Patch HubSpot line item properties."""
    if mock:
        base_props = {
            "name": "Property Management - Monthly Retainer",
            "quantity": "12",
            "price": "10000",
            "amount": "120000",
        }
        return {
            "id": str(line_item_id),
            "properties": {
                **base_props,
                "hs_object_id": str(line_item_id),
                **properties,
            },
            "createdAt": "2026-04-10T08:00:00Z",
            "updatedAt": "2026-04-10T08:00:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/line_items/{line_item_id}",
        {"properties": properties},
    )


@custom_function()
async def sync_hubspot_line_items(
    since: str | None = None,
    schema_version: str = "hubspot.crm.line_items.v1",
    mock: bool = True,
) -> dict:
    """Sync line items modified since ``since`` into a tables envelope."""
    if mock:
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
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

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
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
