"""HubSpot Chatflows - chatbot definitions (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_CHATFLOW = {
    "id": "cf-7001",
    "name": "Property Inquiry Bot",
    "type": "BOT",
    "createdAt": "2025-10-01T10:00:00Z",
    "updatedAt": "2026-03-01T12:00:00Z",
}


@custom_function()
async def list_chatflows(after: str | None = None, limit: int = 50, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_CHATFLOW, "id": f"cf-{7000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/conversations/v3/conversations/chatflows", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_chatflow(chatflow_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_CHATFLOW, "id": str(chatflow_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/conversations/v3/conversations/chatflows/{chatflow_id}")


@custom_function()
async def sync_chatflows(
    schema_version: str = "hubspot.service.chatflows.v1",
    mock: bool = True,
) -> dict:
    if mock:
        rows = [{
            "chatflow_id": f"cf-{7000 + i}", "name": _MOCK_CHATFLOW["name"],
            "type": _MOCK_CHATFLOW["type"],
            "created_at": _MOCK_CHATFLOW["createdAt"],
            "updated_at": _MOCK_CHATFLOW["updatedAt"],
        } for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"chatflows": rows},
            "metadata": {"object_type": "chatflows", "mode": "mock",
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
        body = await hubspot_get("/conversations/v3/conversations/chatflows", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"chatflows": rows},
                    "metadata": {"object_type": "chatflows", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        for r in body.get("results", []):
            rows.append({
                "chatflow_id": str(r.get("id", "")),
                "name": r.get("name", ""), "type": r.get("type", ""),
                "created_at": r.get("createdAt", ""),
                "updated_at": r.get("updatedAt", ""),
            })
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"chatflows": rows},
        "metadata": {"object_type": "chatflows", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
