"""HubSpot cms_pages sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_cms_pages(
    schema_version: str = "hubspot.cms.pages.v1",
    mock: bool = True,
) -> dict:
    """Sync CMS site pages into a tables envelope."""
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_cms_page,
    )

    if mock:
        base = {
            "name": "Sunset Tower - Property Listing",
            "slug": "properties/sunset-tower",
            "url": "https://example.com/properties/sunset-tower",
            "htmlTitle": "Sunset Tower - Luxury Multifamily | Example",
            "metaDescription": "Discover Sunset Tower.",
            "currentState": "PUBLISHED",
            "publishDate": "2025-12-15T10:00:00Z",
            "created": "2025-11-01T10:00:00Z",
            "updated": "2026-04-15T12:00:00Z",
        }
        rows = [normalize_cms_page({**base, "id": f"pg-{8000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"cms_pages": rows},
            "metadata": {
                "object_type": "cms_pages",
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
        body = await hubspot_get("/cms/v3/pages/site-pages", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"cms_pages": rows},
                "metadata": {
                    "object_type": "cms_pages",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        rows.extend(normalize_cms_page(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"cms_pages": rows},
        "metadata": {
            "object_type": "cms_pages",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
