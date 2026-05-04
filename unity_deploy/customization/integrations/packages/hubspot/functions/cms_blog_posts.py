"""HubSpot CMS - Blog Posts CRUD + sync (publish gated)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_blog_posts(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    """Paginate through blog posts."""
    if mock:
        base = {
            "name": "Spring Maintenance Tips for Property Owners",
            "slug": "spring-maintenance-tips",
            "url": "https://example.com/blog/spring-maintenance-tips",
            "htmlTitle": "Spring Maintenance Tips for Property Owners",
            "metaDescription": "Practical tips owners should action before peak leasing season.",
            "blogAuthorId": "ba-9001",
            "currentState": "PUBLISHED",
            "publishDate": "2026-03-15T10:00:00Z",
            "postSummary": "Five maintenance items to action this spring.",
            "tagIds": [101, 102],
            "created": "2026-03-01T10:00:00Z",
            "updated": "2026-04-01T12:00:00Z",
        }
        return {
            "results": [{**base, "id": f"bp-{8500 + i}"} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/cms/v3/blogs/posts", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_blog_post(post_id: str, mock: bool = True) -> dict:
    """Fetch a blog post by ID."""
    if mock:
        return {
            "id": str(post_id),
            "name": "Spring Maintenance Tips for Property Owners",
            "slug": "spring-maintenance-tips",
            "url": "https://example.com/blog/spring-maintenance-tips",
            "htmlTitle": "Spring Maintenance Tips for Property Owners",
            "metaDescription": "Practical tips owners should action before peak leasing season.",
            "blogAuthorId": "ba-9001",
            "currentState": "PUBLISHED",
            "publishDate": "2026-03-15T10:00:00Z",
            "postSummary": "Five maintenance items to action this spring.",
            "tagIds": [101, 102],
            "created": "2026-03-01T10:00:00Z",
            "updated": "2026-04-01T12:00:00Z",
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/cms/v3/blogs/posts/{post_id}")


@custom_function()
async def create_blog_post(
    name: str,
    slug: str,
    html_title: str,
    body_html: str,
    blog_author_id: str | None = None,
    meta_description: str = "",
    post_summary: str = "",
    tag_ids: list[int] | None = None,
    mock: bool = True,
) -> dict:
    """Create a blog post in DRAFT state."""
    if mock:
        return {
            "id": "bp-99001",
            "name": name, "slug": slug, "htmlTitle": html_title,
            "metaDescription": meta_description,
            "blogAuthorId": blog_author_id, "postSummary": post_summary,
            "tagIds": tag_ids or [],
            "currentState": "DRAFT",
            "url": "",
            "publishDate": "",
            "created": "2026-03-01T10:00:00Z",
            "updated": "2026-04-01T12:00:00Z",
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    body: dict = {
        "name": name, "slug": slug,
        "htmlTitle": html_title, "metaDescription": meta_description,
        "postBody": body_html, "postSummary": post_summary,
        "currentState": "DRAFT",
    }
    if blog_author_id is not None:
        body["blogAuthorId"] = blog_author_id
    if tag_ids:
        body["tagIds"] = tag_ids
    return await hubspot_post("/cms/v3/blogs/posts", body)


@custom_function()
async def update_blog_post(post_id: str, properties: dict, mock: bool = True) -> dict:
    """Patch blog post properties."""
    if mock:
        base = {
            "name": "Spring Maintenance Tips for Property Owners",
            "slug": "spring-maintenance-tips",
            "currentState": "PUBLISHED",
        }
        return {**base, "id": str(post_id), **properties}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(f"/cms/v3/blogs/posts/{post_id}", properties)


@custom_function()
async def publish_blog_post(
    post_id: str,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """HIGH-STAKES.  Requires confirm=True AND HUBSPOT_ALLOW_CMS_PUBLISH=true."""
    if not confirm:
        return {"error": "publish_blog_post requires confirm=True.", "post_id": str(post_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_cms_publish"]:
        return {"error": "CMS publish disabled.  Set HUBSPOT_ALLOW_CMS_PUBLISH=true.",
                "post_id": str(post_id)}

    if mock:
        base = {
            "name": "Spring Maintenance Tips for Property Owners",
            "slug": "spring-maintenance-tips",
        }
        return {**base, "id": str(post_id), "currentState": "PUBLISHED"}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/cms/v3/blogs/posts/{post_id}",
        {"currentState": "PUBLISHED"},
    )


@custom_function()
async def sync_blog_posts(
    schema_version: str = "hubspot.cms.blog_posts.v1",
    mock: bool = True,
) -> dict:
    """Sync blog posts into a tables envelope."""
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
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
            "metadata": {"object_type": "blog_posts", "mode": "mock",
                         "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
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
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"blog_posts": rows},
                    "metadata": {"object_type": "blog_posts", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        rows.extend(normalize_blog_post(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"blog_posts": rows},
        "metadata": {"object_type": "blog_posts", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
