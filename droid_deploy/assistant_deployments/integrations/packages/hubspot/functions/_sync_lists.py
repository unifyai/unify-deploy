"""HubSpot lists sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_lists(
    schema_version: str = "hubspot.crm.lists.v1",
    mock: bool = True,
) -> dict:
    """Sync list definitions and (small) memberships.  Heavy memberships
    should run as a separate scheduled task; v0 syncs definitions only."""
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_list,
    )

    if mock:
        rows = [
            normalize_list(
                {
                    "listId": str(400 + i),
                    "name": "Q3 Prospective Tenants",
                    "listType": "STATIC",
                    "processingType": "MANUAL",
                    "createdAt": "2026-04-01T10:00:00Z",
                    "updatedAt": "2026-04-25T15:00:00Z",
                },
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"lists": rows, "list_membership": []},
            "metadata": {
                "object_type": "lists",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    rows: list[dict] = []
    offset = 0
    while True:
        body = await hubspot_post(
            "/crm/v3/lists/search",
            {"count": 250, "offset": offset},
        )
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"lists": rows, "list_membership": []},
                "metadata": {
                    "object_type": "lists",
                    "mode": "real",
                    "row_count": len(rows),
                    "partial": True,
                },
            }
        rows.extend(normalize_list(r) for r in body.get("lists", []))
        if not body.get("hasMore"):
            break
        offset = body.get("offset", offset + 250)

    return {
        "schema_version": schema_version,
        "tables": {"lists": rows, "list_membership": []},
        "metadata": {"object_type": "lists", "mode": "real", "row_count": len(rows)},
    }
