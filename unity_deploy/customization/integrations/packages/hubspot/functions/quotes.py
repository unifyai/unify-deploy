"""HubSpot Quotes - quote generation + sending (high-stakes)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_DEFAULT_PROPERTIES = [
    "hs_title", "hs_status", "hs_expiration_date", "hs_quote_total_preference",
    "hs_terms", "hs_public_url_key", "hubspot_owner_id", "createdate",
    "hs_lastmodifieddate", "hs_object_id",
]

_MOCK_QUOTE = {
    "id": "6001",
    "properties": {
        "hs_title": "Acme Properties - Q3 2026 Management Quote",
        "hs_status": "DRAFT",
        "hs_expiration_date": "2026-07-31",
        "hs_quote_total_preference": "TOTAL",
        "hs_terms": "Net 30; renewable annually.",
        "hs_public_url_key": "abc123",
        "hubspot_owner_id": "60001",
        "createdate": "2026-04-22T10:00:00Z",
        "hs_lastmodifieddate": "2026-04-22T10:00:00Z",
        "hs_object_id": "6001",
    },
    "createdAt": "2026-04-22T10:00:00Z",
    "updatedAt": "2026-04-22T10:00:00Z",
    "archived": False,
}


@custom_function()
async def get_quote(quote_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_QUOTE, "id": str(quote_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(
        f"/crm/v3/objects/quotes/{quote_id}",
        params={"properties": ",".join(_DEFAULT_PROPERTIES)},
    )


@custom_function()
async def list_quotes(after: str | None = None, limit: int = 25, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_QUOTE, "id": str(6000 + i)} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100), "properties": ",".join(_DEFAULT_PROPERTIES)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/quotes", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def create_quote(properties: dict, mock: bool = True) -> dict:
    """Create a quote.  At minimum supply ``hs_title`` and ``hs_expiration_date``."""
    if mock:
        return {**_MOCK_QUOTE, "id": "99001",
                "properties": {**_MOCK_QUOTE["properties"], **properties}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post("/crm/v3/objects/quotes", {"properties": properties})


@custom_function()
async def update_quote(quote_id: str, properties: dict, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_QUOTE, "id": str(quote_id),
                "properties": {**_MOCK_QUOTE["properties"], **properties}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/quotes/{quote_id}",
        {"properties": properties},
    )


@custom_function()
async def send_quote(
    quote_id: str,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Send a HubSpot quote to its associated contacts.  HIGH-STAKES: emails
    the customer.  Requires ``confirm=True`` AND HubSpot quote in ``APPROVAL_NOT_NEEDED`` /
    ``PENDING_APPROVAL`` state."""
    if not confirm:
        return {
            "error": "send_quote requires confirm=True.  Confirm with the user that "
                     "the quote contents and recipients are correct first.",
            "id": str(quote_id),
        }
    if mock:
        return {**_MOCK_QUOTE, "id": str(quote_id),
                "properties": {**_MOCK_QUOTE["properties"], "hs_status": "PENDING_BUYER_SIGNATURE"}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/quotes/{quote_id}",
        {"properties": {"hs_status": "PENDING_BUYER_SIGNATURE"}},
    )


@custom_function()
async def sync_quotes(
    since: str | None = None,
    schema_version: str = "hubspot.crm.quotes.v1",
    mock: bool = True,
) -> dict:
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_quote,
        )
        rows = [normalize_quote({**_MOCK_QUOTE, "id": str(6000 + i)}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"quotes": rows},
            "metadata": {"object_type": "quotes", "mode": "mock",
                         "since": since, "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_quote,
    )

    cfg = get_hubspot_config()
    filter_groups = (
        [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": since}]}]
        if since else []
    )
    rows, page_count, after = [], 0, None
    while True:
        body = await hubspot_search(
            "quotes",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=_DEFAULT_PROPERTIES,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"quotes": rows},
                    "metadata": {"object_type": "quotes", "mode": "real",
                                 "since": since, "row_count": len(rows),
                                 "pages": page_count, "partial": True}}
        rows.extend(normalize_quote(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]):
            break
    return {
        "schema_version": schema_version,
        "tables": {"quotes": rows},
        "metadata": {"object_type": "quotes", "mode": "real",
                     "since": since, "row_count": len(rows), "pages": page_count},
    }
