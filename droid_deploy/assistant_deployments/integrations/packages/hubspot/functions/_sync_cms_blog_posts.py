"""HubSpot cms_blog_posts sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_blog_posts(
    schema_version: str = "hubspot.cms.blog_posts.v1",
    mock: bool = True,
) -> dict:
    """Sync blog posts into a tables envelope."""
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_blog_post,
    )

    if mock:
        base = {
            "name": "Spring Maintenance Tips for Property Owners",
            "slug": "spring-maintenance-tips",
            "url": "https://example.com/blog/spring-maintenance-tips",
            "htmlTitle": "Spring Maintenance Tips for Property Owners",
            "metaDescription": "Practical tips owners should action.",
            "blogAuthorId": "ba-9001",
            "currentState": "PUBLISHED",
            "publishDate": "2026-03-15T10:00:00Z",
            "postSummary": "Five maintenance items.",
            "tagIds": [101, 102],
            "created": "2026-03-01T10:00:00Z",
            "updated": "2026-04-01T12:00:00Z",
        }
        rows = [normalize_blog_post({**base, "id": f"bp-{8500 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"blog_posts": rows},
            "metadata": {
                "object_type": "blog_posts",
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
        body = await hubspot_get("/cms/v3/blogs/posts", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"blog_posts": rows},
                "metadata": {
                    "object_type": "blog_posts",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        rows.extend(normalize_blog_post(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"blog_posts": rows},
        "metadata": {
            "object_type": "blog_posts",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
