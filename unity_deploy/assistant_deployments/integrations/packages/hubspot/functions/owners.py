"""HubSpot Owners - HubSpot users who own records (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_owners(
    after: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """List HubSpot users who can own CRM records."""
    if mock:
        return {
            "results": [
                {
                    "id": str(60000 + i),
                    "email": f"owner{i}@example.com",
                    "firstName": "Sample",
                    "lastName": "Owner",
                    "userId": 5000 + i,
                    "teams": [{"id": "1", "name": "Sales"}],
                    "createdAt": "2025-01-15T10:00:00Z",
                    "updatedAt": "2026-04-15T10:00:00Z",
                    "archived": False,
                }
                for i in range(min(limit, 5))
            ],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 500)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/owners", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def sync_owners(
    schema_version: str = "hubspot.crm.owners.v1",
    mock: bool = True,
) -> dict:
    """Pull all HubSpot owners (small set - full pull each tick)."""
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_owner,
    )

    if mock:
        rows = [
            normalize_owner(
                {
                    "id": str(60000 + i),
                    "email": f"owner{i}@example.com",
                    "firstName": "Sample",
                    "lastName": "Owner",
                    "userId": 5000 + i,
                    "teams": [{"id": "1", "name": "Sales"}],
                    "createdAt": "2025-01-15T10:00:00Z",
                    "updatedAt": "2026-04-15T10:00:00Z",
                    "archived": False,
                },
            )
            for i in range(5)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"owners": rows},
            "metadata": {
                "object_type": "owners",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    rows: list[dict] = []
    after: str | None = None
    pages = 0
    while True:
        params: dict = {"limit": 500}
        if after:
            params["after"] = after
        body = await hubspot_get("/crm/v3/owners", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"owners": rows},
                "metadata": {
                    "object_type": "owners",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        rows.extend(normalize_owner(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"owners": rows},
        "metadata": {
            "object_type": "owners",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
