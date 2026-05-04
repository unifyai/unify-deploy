"""HubSpot CMS - URL Redirects (CRUD)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_REDIRECT = {
    "id": "rd-12001",
    "routePrefix": "/old-properties/sunset-tower",
    "destination": "https://example.com/properties/sunset-tower",
    "redirectStyle": 301,
    "isOnlyAfterNotFound": False,
    "createdAt": "2026-01-15T10:00:00Z",
    "updatedAt": "2026-01-15T10:00:00Z",
}


@custom_function()
async def list_url_redirects(after: str | None = None, limit: int = 50, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_REDIRECT, "id": f"rd-{12000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/cms/v3/url-redirects", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def create_url_redirect(
    route_prefix: str,
    destination: str,
    redirect_style: int = 301,
    mock: bool = True,
) -> dict:
    if mock:
        return {**_MOCK_REDIRECT, "id": "rd-99001",
                "routePrefix": route_prefix, "destination": destination,
                "redirectStyle": redirect_style}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        "/cms/v3/url-redirects",
        {"routePrefix": route_prefix, "destination": destination,
         "redirectStyle": redirect_style},
    )


@custom_function()
async def update_url_redirect(redirect_id: str, properties: dict, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_REDIRECT, "id": str(redirect_id), **properties}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(f"/cms/v3/url-redirects/{redirect_id}", properties)


@custom_function()
async def delete_url_redirect(redirect_id: str, mock: bool = True) -> dict:
    if mock:
        return {"status": "deleted", "id": str(redirect_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_delete"]:
        return {"error": "Deletes disabled.", "id": str(redirect_id)}
    return await hubspot_delete(f"/cms/v3/url-redirects/{redirect_id}")


@custom_function()
async def sync_url_redirects(
    schema_version: str = "hubspot.cms.url_redirects.v1",
    mock: bool = True,
) -> dict:
    if mock:
        rows = [{
            "redirect_id": f"rd-{12000 + i}",
            "route_prefix": _MOCK_REDIRECT["routePrefix"],
            "destination": _MOCK_REDIRECT["destination"],
            "redirect_style": _MOCK_REDIRECT["redirectStyle"],
            "created_at": _MOCK_REDIRECT["createdAt"],
            "updated_at": _MOCK_REDIRECT["updatedAt"],
        } for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"url_redirects": rows},
            "metadata": {"object_type": "url_redirects", "mode": "mock",
                         "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    rows: list[dict] = []
    after: str | None = None
    pages = 0
    while True:
        params: dict = {"limit": 100}
        if after:
            params["after"] = after
        body = await hubspot_get("/cms/v3/url-redirects", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"url_redirects": rows},
                    "metadata": {"object_type": "url_redirects", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        for r in body.get("results", []):
            rows.append({
                "redirect_id": str(r.get("id", "")),
                "route_prefix": r.get("routePrefix", ""),
                "destination": r.get("destination", ""),
                "redirect_style": r.get("redirectStyle"),
                "created_at": r.get("createdAt", ""),
                "updated_at": r.get("updatedAt", ""),
            })
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"url_redirects": rows},
        "metadata": {"object_type": "url_redirects", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
