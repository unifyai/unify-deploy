"""HubSpot CMS - Blog Authors (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_blog_authors(after: str | None = None, limit: int = 50, mock: bool = True) -> dict:
    """List blog authors."""
    if mock:
        base = {
            "name": "Sample Author",
            "email": "author@example.com",
            "fullName": "Sample Author",
            "slug": "sample-author",
            "createdAt": "2025-09-01T10:00:00Z",
            "updatedAt": "2026-01-15T12:00:00Z",
        }
        return {
            "results": [{**base, "id": f"ba-{9000 + i}"} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/cms/v3/blogs/authors", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}
