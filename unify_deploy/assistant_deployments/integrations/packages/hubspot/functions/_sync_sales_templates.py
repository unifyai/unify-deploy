"""HubSpot sales_templates sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_sales_templates(
    schema_version: str = "hubspot.sales.templates.v1",
    mock: bool = True,
) -> dict:
    """Sync sales template metadata into a tables envelope."""
    if mock:
        rows = [
            {
                "template_id": f"tpl-{9500 + i}",
                "name": "Initial Owner Outreach",
                "subject": "Quick question about {{company.name}}",
                "body_preview": "Hi {{contact.firstname}},\n\nI noticed your portfolio includes ...",
                "created_at": "2025-10-01T10:00:00Z",
                "updated_at": "2026-02-15T12:00:00Z",
            }
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"sales_templates": rows},
            "metadata": {
                "object_type": "sales_templates",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    rows: list[dict] = []
    after: str | None = None
    pages = 0
    while True:
        params: dict = {"limit": 100}
        if after:
            params["after"] = after
        body = await hubspot_get("/marketing/v3/transactional/templates", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"sales_templates": rows},
                "metadata": {
                    "object_type": "sales_templates",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        for r in body.get("results", []):
            rows.append(
                {
                    "template_id": str(r.get("id", "")),
                    "name": r.get("name", ""),
                    "subject": r.get("subject", ""),
                    "body_preview": (r.get("body") or "")[:200],
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
        "tables": {"sales_templates": rows},
        "metadata": {
            "object_type": "sales_templates",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
