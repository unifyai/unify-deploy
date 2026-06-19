"""HubSpot marketing_workflows sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_marketing_workflows(
    schema_version: str = "hubspot.marketing.workflows.v1",
    mock: bool = True,
) -> dict:
    """Sync marketing workflow definitions into a tables envelope."""
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_workflow,
    )

    if mock:
        base = {
            "name": "Prospective Tenant Nurture",
            "type": "DRIP_DELAY",
            "enabled": True,
            "createdAt": "2025-10-01T09:00:00Z",
            "updatedAt": "2026-04-01T11:00:00Z",
        }
        rows = [normalize_workflow({**base, "id": f"wf-{3000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"marketing_workflows": rows},
            "metadata": {
                "object_type": "marketing_workflows",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    rows: list[dict] = []
    after: str | None = None
    pages = 0
    while True:
        params: dict = {"limit": 100}
        if after:
            params["after"] = after
        body = await hubspot_get("/automation/v4/flows", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"marketing_workflows": rows},
                "metadata": {
                    "object_type": "marketing_workflows",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        rows.extend(normalize_workflow(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"marketing_workflows": rows},
        "metadata": {
            "object_type": "marketing_workflows",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
