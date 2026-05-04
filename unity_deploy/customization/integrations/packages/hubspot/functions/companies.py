"""HubSpot Companies - on-demand CRUD + sync."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_DEFAULT_PROPERTIES = [
    "name", "domain", "industry", "phone", "city", "state", "country",
    "numberofemployees", "annualrevenue", "lifecyclestage", "type",
    "createdate", "hs_lastmodifieddate", "hs_object_id",
]

_MOCK_COMPANY = {
    "id": "5001",
    "properties": {
        "name": "Acme Properties LLC",
        "domain": "acme-properties.com",
        "industry": "REAL_ESTATE",
        "phone": "+1 555 200 0100",
        "city": "Tampa",
        "state": "FL",
        "country": "United States",
        "numberofemployees": "120",
        "annualrevenue": "8000000",
        "lifecyclestage": "customer",
        "type": "PROSPECT",
        "createdate": "2025-09-01T09:00:00Z",
        "hs_lastmodifieddate": "2026-04-20T11:00:00Z",
        "hs_object_id": "5001",
    },
    "createdAt": "2025-09-01T09:00:00Z",
    "updatedAt": "2026-04-20T11:00:00Z",
    "archived": False,
}


@custom_function()
async def get_company(company_id: str, mock: bool = True) -> dict:
    """Fetch a single HubSpot company by ID."""
    if mock:
        return {**_MOCK_COMPANY, "id": str(company_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    cfg = get_hubspot_config()
    properties = (
        ",".join(cfg["company_properties"])
        if isinstance(cfg["company_properties"], list)
        else ",".join(_DEFAULT_PROPERTIES)
    )
    return await hubspot_get(
        f"/crm/v3/objects/companies/{company_id}",
        params={"properties": properties, "archived": "false"},
    )


@custom_function()
async def search_companies(query: str, limit: int = 10, mock: bool = True) -> dict:
    """Full-text search HubSpot companies."""
    if mock:
        return {"results": [{**_MOCK_COMPANY, "id": "5001"}], "total": 1}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )

    return await hubspot_search(
        "companies",
        query=query,
        properties=_DEFAULT_PROPERTIES,
        limit=min(limit, 100),
    )


@custom_function()
async def list_companies(after: str | None = None, limit: int = 25, mock: bool = True) -> dict:
    """Paginate through HubSpot companies."""
    if mock:
        return {
            "results": [{**_MOCK_COMPANY, "id": str(5000 + i)} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100), "properties": ",".join(_DEFAULT_PROPERTIES)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/companies", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def create_company(properties: dict, mock: bool = True) -> dict:
    """Create a HubSpot company.  At minimum supply ``name`` or ``domain``."""
    if mock:
        return {**_MOCK_COMPANY, "id": "99001",
                "properties": {**_MOCK_COMPANY["properties"], **properties}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post("/crm/v3/objects/companies", {"properties": properties})


@custom_function()
async def update_company(company_id: str, properties: dict, mock: bool = True) -> dict:
    """Patch HubSpot company properties."""
    if mock:
        return {**_MOCK_COMPANY, "id": str(company_id),
                "properties": {**_MOCK_COMPANY["properties"], **properties}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/companies/{company_id}",
        {"properties": properties},
    )


@custom_function()
async def delete_company(company_id: str, mock: bool = True) -> dict:
    """Soft-delete a HubSpot company.  Gated by HUBSPOT_ALLOW_DELETE."""
    if mock:
        return {"status": "deleted", "id": str(company_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_delete"]:
        return {
            "error": "Deletes are disabled.  Set HUBSPOT_ALLOW_DELETE=true to enable.",
            "id": str(company_id),
        }
    return await hubspot_delete(f"/crm/v3/objects/companies/{company_id}")


@custom_function()
async def sync_companies(
    since: str | None = None,
    schema_version: str = "hubspot.crm.companies.v1",
    mock: bool = True,
) -> dict:
    """Sync companies modified since ``since`` into a tables envelope."""
    if mock:
        from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
            normalize_company,
        )
        rows = [normalize_company({**_MOCK_COMPANY, "id": str(5000 + i)}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"companies": rows},
            "metadata": {"object_type": "companies", "mode": "mock",
                         "since": since, "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_company,
    )

    cfg = get_hubspot_config()
    properties = (
        cfg["company_properties"]
        if isinstance(cfg["company_properties"], list)
        else _DEFAULT_PROPERTIES
    )
    filter_groups = (
        [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": since}]}]
        if since else []
    )
    rows, page_count, after = [], 0, None
    while True:
        body = await hubspot_search(
            "companies",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=properties,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"companies": rows},
                    "metadata": {"object_type": "companies", "mode": "real",
                                 "since": since, "row_count": len(rows),
                                 "pages": page_count, "partial": True}}
        rows.extend(normalize_company(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]):
            break
    return {
        "schema_version": schema_version,
        "tables": {"companies": rows},
        "metadata": {"object_type": "companies", "mode": "real",
                     "since": since, "row_count": len(rows), "pages": page_count},
    }
