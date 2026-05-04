"""HubSpot Goals - sales / service goal tracking (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_GOAL = {
    "id": "G1001",
    "properties": {
        "hs_goal_name": "Q3 Lease Conversions",
        "hs_goal_target": "50",
        "hs_goal_progress": "32",
        "hs_owner_id": "60001",
        "hs_period_start": "2026-07-01",
        "hs_period_end": "2026-09-30",
    },
    "createdAt": "2026-06-15T10:00:00Z",
    "updatedAt": "2026-09-01T15:00:00Z",
    "archived": False,
}


@custom_function()
async def list_goals(after: str | None = None, limit: int = 25, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_GOAL, "id": f"G{1000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/goal_targets", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def sync_goals(
    schema_version: str = "hubspot.crm.goals.v1",
    mock: bool = True,
) -> dict:
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_object,
    )

    if mock:
        rows = [normalize_object({**_MOCK_GOAL, "id": f"G{1000 + i}"},
                                 object_type="goal") for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"goals": rows},
            "metadata": {"object_type": "goals", "mode": "mock", "row_count": len(rows)},
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
        body = await hubspot_get("/crm/v3/objects/goal_targets", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"goals": rows},
                    "metadata": {"object_type": "goals", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        rows.extend(normalize_object(r, object_type="goal") for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"goals": rows},
        "metadata": {"object_type": "goals", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
