"""HubSpot Knowledge Base Articles - browse + CRUD."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_ARTICLE = {
    "id": "kb-6001",
    "title": "How do I submit a maintenance request?",
    "slug": "how-to-submit-maintenance-request",
    "language": "en",
    "categoryId": 1, "subcategoryId": None,
    "currentState": "PUBLISHED",
    "url": "https://example.com/kb/how-to-submit-maintenance-request",
    "htmlTitle": "Maintenance Requests | Help Center",
    "metaDescription": "Steps to submit a maintenance request for your unit.",
    "created": "2025-09-15T10:00:00Z",
    "updated": "2026-04-01T12:00:00Z",
}


@custom_function()
async def list_kb_articles(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    if mock:
        return {"results": [{**_MOCK_ARTICLE, "id": f"kb-{6000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/cms/v3/knowledge-base/articles", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_kb_article(article_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_ARTICLE, "id": str(article_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/cms/v3/knowledge-base/articles/{article_id}")


@custom_function()
async def create_kb_article(
    title: str,
    content_html: str,
    slug: str,
    language: str = "en",
    category_id: int | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {**_MOCK_ARTICLE, "id": "kb-99001",
                "title": title, "slug": slug, "language": language,
                "categoryId": category_id, "currentState": "DRAFT"}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    body = {
        "title": title, "slug": slug, "language": language,
        "content": content_html, "currentState": "DRAFT",
    }
    if category_id is not None:
        body["categoryId"] = category_id
    return await hubspot_post("/cms/v3/knowledge-base/articles", body)


@custom_function()
async def update_kb_article(article_id: str, properties: dict, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_ARTICLE, "id": str(article_id), **properties}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(f"/cms/v3/knowledge-base/articles/{article_id}", properties)


@custom_function()
async def sync_kb_articles(
    schema_version: str = "hubspot.service.kb_articles.v1",
    mock: bool = True,
) -> dict:
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_kb_article,
    )

    if mock:
        rows = [normalize_kb_article({**_MOCK_ARTICLE, "id": f"kb-{6000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"kb_articles": rows},
            "metadata": {"object_type": "kb_articles", "mode": "mock",
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
        body = await hubspot_get("/cms/v3/knowledge-base/articles", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"kb_articles": rows},
                    "metadata": {"object_type": "kb_articles", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        rows.extend(normalize_kb_article(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"kb_articles": rows},
        "metadata": {"object_type": "kb_articles", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
