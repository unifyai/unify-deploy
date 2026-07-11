"""HubSpot sales_documents sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_sales_documents(
    schema_version: str = "hubspot.sales.documents.v1",
    mock: bool = True,
) -> dict:
    """Sync sales document metadata into a tables envelope."""
    if mock:
        rows = [
            {
                "document_id": f"doc-{2000 + i}",
                "name": "Property Management Services Brochure",
                "size": 2456789,
                "shareable_url": "https://example.com/docs/pm-brochure",
                "created_at": "2025-09-01T10:00:00Z",
                "updated_at": "2026-01-15T12:00:00Z",
            }
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"sales_documents": rows},
            "metadata": {
                "object_type": "sales_documents",
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
        params: dict = {"limit": 100, "category": "DOCUMENT"}
        if after:
            params["after"] = after
        body = await hubspot_get("/files/v3/files", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"sales_documents": rows},
                "metadata": {
                    "object_type": "sales_documents",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        for r in body.get("results", []):
            rows.append(
                {
                    "document_id": str(r.get("id", "")),
                    "name": r.get("name", ""),
                    "size": r.get("size"),
                    "shareable_url": r.get("url", ""),
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
        "tables": {"sales_documents": rows},
        "metadata": {
            "object_type": "sales_documents",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
