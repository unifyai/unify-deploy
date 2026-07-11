"""HubSpot service_knowledge_base sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_kb_articles(
    schema_version: str = "hubspot.service.kb_articles.v1",
    mock: bool = True,
) -> dict:
    """Sync KB articles into a tables envelope."""
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_kb_article,
    )

    if mock:
        base = {
            "title": "How do I submit a maintenance request?",
            "slug": "how-to-submit-maintenance-request",
            "language": "en",
            "currentState": "PUBLISHED",
            "url": "https://example.com/kb/how-to-submit-maintenance-request",
            "htmlTitle": "Maintenance Requests | Help Center",
            "metaDescription": "Steps to submit a maintenance request for your unit.",
            "created": "2025-09-15T10:00:00Z",
            "updated": "2026-04-01T12:00:00Z",
        }
        rows = [
            normalize_kb_article({**base, "id": f"kb-{6000 + i}"}) for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"kb_articles": rows},
            "metadata": {
                "object_type": "kb_articles",
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
        body = await hubspot_get("/cms/v3/knowledge-base/articles", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"kb_articles": rows},
                "metadata": {
                    "object_type": "kb_articles",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        rows.extend(normalize_kb_article(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"kb_articles": rows},
        "metadata": {
            "object_type": "kb_articles",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
