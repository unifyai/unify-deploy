"""HubSpot Sales Documents - document library + view tracking (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_DOCUMENT = {
    "id": "doc-2001",
    "name": "Property Management Services Brochure",
    "createdAt": "2025-09-01T10:00:00Z",
    "updatedAt": "2026-01-15T12:00:00Z",
    "size": 2456789,
    "shareableUrl": "https://example.com/docs/pm-brochure",
}


@custom_function()
async def list_sales_documents(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    if mock:
        return {"results": [{**_MOCK_DOCUMENT, "id": f"doc-{2000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/files/v3/files", params={**params, "category": "DOCUMENT"})
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_sales_document(document_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_DOCUMENT, "id": str(document_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/files/v3/files/{document_id}")


@custom_function()
async def get_document_view_summary(document_id: str, mock: bool = True) -> dict:
    """Aggregate view-tracking summary for a sales document."""
    if mock:
        return {
            "document_id": str(document_id),
            "total_views": 42, "unique_viewers": 18,
            "first_view_at": "2026-04-10T10:00:00Z",
            "last_view_at": "2026-04-26T14:30:00Z",
        }

    return {
        "document_id": str(document_id),
        "note": ("Document view-tracking summaries require the documents-tracking "
                 "scope; v0 is mock-only.  Drive from the v4 events endpoint when needed."),
    }


@custom_function()
async def sync_sales_documents(
    schema_version: str = "hubspot.sales.documents.v1",
    mock: bool = True,
) -> dict:
    if mock:
        rows = [{
            "document_id": f"doc-{2000 + i}",
            "name": _MOCK_DOCUMENT["name"], "size": _MOCK_DOCUMENT["size"],
            "shareable_url": _MOCK_DOCUMENT["shareableUrl"],
            "created_at": _MOCK_DOCUMENT["createdAt"],
            "updated_at": _MOCK_DOCUMENT["updatedAt"],
        } for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"sales_documents": rows},
            "metadata": {"object_type": "sales_documents", "mode": "mock",
                         "row_count": len(rows)},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
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
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"sales_documents": rows},
                    "metadata": {"object_type": "sales_documents", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        for r in body.get("results", []):
            rows.append({
                "document_id": str(r.get("id", "")),
                "name": r.get("name", ""), "size": r.get("size"),
                "shareable_url": r.get("url", ""),
                "created_at": r.get("createdAt", ""),
                "updated_at": r.get("updatedAt", ""),
            })
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"sales_documents": rows},
        "metadata": {"object_type": "sales_documents", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
