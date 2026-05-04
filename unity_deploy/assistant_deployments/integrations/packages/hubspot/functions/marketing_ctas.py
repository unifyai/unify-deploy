"""HubSpot CTAs - call-to-action assets (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_ctas(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    """List marketing CTAs."""
    if mock:
        base = {
            "name": "Schedule a Tour",
            "createdAt": "2025-09-15T10:00:00Z",
            "updatedAt": "2026-03-01T12:00:00Z",
        }
        return {
            "results": [
                {**base, "id": f"cta-{4000 + i}"} for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/cms/v3/cta", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def sync_ctas(
    schema_version: str = "hubspot.marketing.ctas.v1",
    mock: bool = True,
) -> dict:
    """Sync CTA definitions into a tables envelope."""
    if mock:
        rows = [
            {
                "cta_id": f"cta-{4000 + i}",
                "name": "Schedule a Tour",
                "created_at": "2025-09-15T10:00:00Z",
                "updated_at": "2026-03-01T12:00:00Z",
            }
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"marketing_ctas": rows},
            "metadata": {
                "object_type": "marketing_ctas",
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
        params: dict = {"limit": 100}
        if after:
            params["after"] = after
        body = await hubspot_get("/cms/v3/cta", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"marketing_ctas": rows},
                "metadata": {
                    "object_type": "marketing_ctas",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        for r in body.get("results", []):
            rows.append(
                {
                    "cta_id": str(r.get("id", "")),
                    "name": r.get("name", ""),
                    "created_at": r.get("createdAt", ""),
                    "updated_at": r.get("updatedAt", ""),
                },
            )
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"marketing_ctas": rows},
        "metadata": {
            "object_type": "marketing_ctas",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
