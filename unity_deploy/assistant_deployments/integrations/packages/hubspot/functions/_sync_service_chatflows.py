"""HubSpot service_chatflows sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_chatflows(
    schema_version: str = "hubspot.service.chatflows.v1",
    mock: bool = True,
) -> dict:
    """Sync chat flow definitions into a tables envelope."""
    if mock:
        rows = [
            {
                "chatflow_id": f"cf-{7000 + i}",
                "name": "Property Inquiry Bot",
                "type": "BOT",
                "created_at": "2025-10-01T10:00:00Z",
                "updated_at": "2026-03-01T12:00:00Z",
            }
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"chatflows": rows},
            "metadata": {
                "object_type": "chatflows",
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
        body = await hubspot_get(
            "/conversations/v3/conversations/chatflows",
            params=params,
        )
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"chatflows": rows},
                "metadata": {
                    "object_type": "chatflows",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        for r in body.get("results", []):
            rows.append(
                {
                    "chatflow_id": str(r.get("id", "")),
                    "name": r.get("name", ""),
                    "type": r.get("type", ""),
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
        "tables": {"chatflows": rows},
        "metadata": {
            "object_type": "chatflows",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
