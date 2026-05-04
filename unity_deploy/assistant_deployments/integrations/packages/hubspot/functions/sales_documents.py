"""HubSpot Sales Documents - document library + view tracking (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_sales_documents(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    """Paginate through sales documents."""
    if mock:
        base = {
            "name": "Property Management Services Brochure",
            "createdAt": "2025-09-01T10:00:00Z",
            "updatedAt": "2026-01-15T12:00:00Z",
            "size": 2456789,
            "shareableUrl": "https://example.com/docs/pm-brochure",
        }
        return {
            "results": [
                {**base, "id": f"doc-{2000 + i}"} for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get(
        "/files/v3/files",
        params={**params, "category": "DOCUMENT"},
    )
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def get_sales_document(document_id: str, mock: bool = True) -> dict:
    """Fetch a sales document by ID."""
    if mock:
        return {
            "id": str(document_id),
            "name": "Property Management Services Brochure",
            "createdAt": "2025-09-01T10:00:00Z",
            "updatedAt": "2026-01-15T12:00:00Z",
            "size": 2456789,
            "shareableUrl": "https://example.com/docs/pm-brochure",
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/files/v3/files/{document_id}")


@custom_function()
async def get_document_view_summary(document_id: str, mock: bool = True) -> dict:
    """Aggregate view-tracking summary for a sales document."""
    if mock:
        return {
            "document_id": str(document_id),
            "total_views": 42,
            "unique_viewers": 18,
            "first_view_at": "2026-04-10T10:00:00Z",
            "last_view_at": "2026-04-26T14:30:00Z",
        }

    return {
        "document_id": str(document_id),
        "note": (
            "Document view-tracking summaries require the documents-tracking "
            "scope; v0 is mock-only."
        ),
    }


@custom_function()
async def sync_sales_documents(
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

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
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
