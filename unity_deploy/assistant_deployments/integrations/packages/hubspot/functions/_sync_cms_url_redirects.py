"""HubSpot cms_url_redirects sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_url_redirects(
    schema_version: str = "hubspot.cms.url_redirects.v1",
    mock: bool = True,
) -> dict:
    """Sync URL redirect definitions into a tables envelope."""
    if mock:
        rows = [
            {
                "redirect_id": f"rd-{12000 + i}",
                "route_prefix": "/old-properties/sunset-tower",
                "destination": "https://example.com/properties/sunset-tower",
                "redirect_style": 301,
                "created_at": "2026-01-15T10:00:00Z",
                "updated_at": "2026-01-15T10:00:00Z",
            }
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"url_redirects": rows},
            "metadata": {
                "object_type": "url_redirects",
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
        body = await hubspot_get("/cms/v3/url-redirects", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"url_redirects": rows},
                "metadata": {
                    "object_type": "url_redirects",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        for r in body.get("results", []):
            rows.append(
                {
                    "redirect_id": str(r.get("id", "")),
                    "route_prefix": r.get("routePrefix", ""),
                    "destination": r.get("destination", ""),
                    "redirect_style": r.get("redirectStyle"),
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
        "tables": {"url_redirects": rows},
        "metadata": {
            "object_type": "url_redirects",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
