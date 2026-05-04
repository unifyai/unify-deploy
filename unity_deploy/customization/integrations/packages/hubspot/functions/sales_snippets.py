"""HubSpot Sales Snippets - reusable text blocks (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_SNIPPET = {
    "id": "snip-1001",
    "shortcut": "managementfee",
    "text": "Our standard management fee is 7% of monthly rents collected, with a $150 onboarding fee per unit.",
    "createdAt": "2025-09-01T10:00:00Z",
    "updatedAt": "2025-12-01T12:00:00Z",
}


@custom_function()
async def list_sales_snippets(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    if mock:
        return {"results": [{**_MOCK_SNIPPET, "id": f"snip-{1000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    # The snippets endpoint isn't currently part of the public v3 API, so fall
    # back gracefully if HubSpot returns 404 - the function is still useful in
    # mock mode for the assistant to reason about snippets conceptually.
    body = await hubspot_get("/sales/v1/snippets")
    if "error" in body:
        return body
    return {"results": body if isinstance(body, list) else body.get("results", []),
            "next_after": None}


@custom_function()
async def get_sales_snippet(snippet_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_SNIPPET, "id": str(snippet_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/sales/v1/snippets/{snippet_id}")


@custom_function()
async def sync_sales_snippets(
    schema_version: str = "hubspot.sales.snippets.v1",
    mock: bool = True,
) -> dict:
    if mock:
        rows = [{
            "snippet_id": f"snip-{1000 + i}",
            "shortcut": _MOCK_SNIPPET["shortcut"],
            "text": _MOCK_SNIPPET["text"],
            "created_at": _MOCK_SNIPPET["createdAt"],
            "updated_at": _MOCK_SNIPPET["updatedAt"],
        } for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"sales_snippets": rows},
            "metadata": {"object_type": "sales_snippets", "mode": "mock",
                         "row_count": len(rows)},
        }

    return {
        "schema_version": schema_version,
        "tables": {"sales_snippets": []},
        "metadata": {"object_type": "sales_snippets", "mode": "real",
                     "row_count": 0,
                     "note": "Snippets API requires a beta scope; v0 is mock-only."},
    }
