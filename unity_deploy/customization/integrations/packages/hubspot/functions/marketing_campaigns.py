"""HubSpot Marketing Campaigns (read-only, tier-gated)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_CAMPAIGN = {
    "id": "cmp-1001",
    "name": "Spring 2026 Tenant Outreach",
    "type": "EMAIL",
    "status": "active",
    "createdAt": "2026-03-01T10:00:00Z",
    "updatedAt": "2026-04-25T15:00:00Z",
}


@custom_function()
async def list_campaigns(after: str | None = None, limit: int = 50, mock: bool = True) -> dict:
    """List marketing campaigns.  Tier-gated (Marketing Hub Pro+)."""
    if mock:
        return {"results": [{**_MOCK_CAMPAIGN, "id": f"cmp-{1000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/marketing/v3/campaigns", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_campaign(campaign_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_CAMPAIGN, "id": str(campaign_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/marketing/v3/campaigns/{campaign_id}")


@custom_function()
async def sync_campaigns(
    schema_version: str = "hubspot.marketing.campaigns.v1",
    mock: bool = True,
) -> dict:
    if mock:
        rows = [{
            "campaign_id": f"cmp-{1000 + i}",
            "name": _MOCK_CAMPAIGN["name"],
            "type": _MOCK_CAMPAIGN["type"],
            "status": _MOCK_CAMPAIGN["status"],
            "created_at": _MOCK_CAMPAIGN["createdAt"],
            "updated_at": _MOCK_CAMPAIGN["updatedAt"],
        } for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"marketing_campaigns": rows},
            "metadata": {"object_type": "marketing_campaigns", "mode": "mock",
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
        body = await hubspot_get("/marketing/v3/campaigns", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"marketing_campaigns": rows},
                    "metadata": {"object_type": "marketing_campaigns", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        for r in body.get("results", []):
            rows.append({
                "campaign_id": str(r.get("id", "")),
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "status": r.get("status", ""),
                "created_at": r.get("createdAt", ""),
                "updated_at": r.get("updatedAt", ""),
            })
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"marketing_campaigns": rows},
        "metadata": {"object_type": "marketing_campaigns", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
