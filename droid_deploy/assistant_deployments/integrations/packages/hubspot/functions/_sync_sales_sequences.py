"""HubSpot sales_sequences sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_sequences(
    schema_version: str = "hubspot.sales.sequences.v1",
    mock: bool = True,
) -> dict:
    """Sync sales sequence definitions into a tables envelope."""
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_sequence,
    )

    if mock:
        base = {
            "name": "Owner Outreach - Discovery",
            "folderId": None,
            "createdAt": "2025-09-15T10:00:00Z",
            "updatedAt": "2026-01-15T11:00:00Z",
        }
        rows = [normalize_sequence({**base, "id": f"seq-{9000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"sales_sequences": rows},
            "metadata": {
                "object_type": "sales_sequences",
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
        body = await hubspot_get("/automation/v4/sequences", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"sales_sequences": rows},
                "metadata": {
                    "object_type": "sales_sequences",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        rows.extend(normalize_sequence(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"sales_sequences": rows},
        "metadata": {
            "object_type": "sales_sequences",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
