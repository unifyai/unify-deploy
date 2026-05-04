"""HubSpot Meeting Links - scheduling pages (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_MEETING_LINK = {
    "id": "ml-3001",
    "name": "Property Tour - 30 minute slot",
    "slug": "tour-30",
    "userIdsOfLinkMembers": ["60001"],
    "type": "PERSONAL",
    "createdAt": "2025-10-01T10:00:00Z",
    "updatedAt": "2026-01-15T12:00:00Z",
}


@custom_function()
async def list_meeting_links(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    if mock:
        return {"results": [{**_MOCK_MEETING_LINK, "id": f"ml-{3000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/scheduler/v3/meetings/meeting-links", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_meeting_link(link_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_MEETING_LINK, "id": str(link_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/scheduler/v3/meetings/meeting-links/{link_id}")


@custom_function()
async def sync_meeting_links(
    schema_version: str = "hubspot.sales.meeting_links.v1",
    mock: bool = True,
) -> dict:
    if mock:
        rows = [{
            "link_id": f"ml-{3000 + i}",
            "name": _MOCK_MEETING_LINK["name"],
            "slug": _MOCK_MEETING_LINK["slug"],
            "type": _MOCK_MEETING_LINK["type"],
            "owners_json": str(_MOCK_MEETING_LINK["userIdsOfLinkMembers"]),
            "created_at": _MOCK_MEETING_LINK["createdAt"],
            "updated_at": _MOCK_MEETING_LINK["updatedAt"],
        } for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"meeting_links": rows},
            "metadata": {"object_type": "meeting_links", "mode": "mock",
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
        body = await hubspot_get("/scheduler/v3/meetings/meeting-links", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"meeting_links": rows},
                    "metadata": {"object_type": "meeting_links", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        for r in body.get("results", []):
            rows.append({
                "link_id": str(r.get("id", "")),
                "name": r.get("name", ""), "slug": r.get("slug", ""),
                "type": r.get("type", ""),
                "owners_json": str(r.get("userIdsOfLinkMembers", [])),
                "created_at": r.get("createdAt", ""),
                "updated_at": r.get("updatedAt", ""),
            })
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"meeting_links": rows},
        "metadata": {"object_type": "meeting_links", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
