"""HubSpot marketing_forms sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_marketing_forms(
    schema_version: str = "hubspot.marketing.forms.v1",
    mock: bool = True,
) -> dict:
    """Sync form definitions into a tables envelope."""
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_form,
    )

    if mock:
        base = {
            "name": "Property Inquiry",
            "formType": "hubspot",
            "createdAt": "2025-10-01T10:00:00Z",
            "updatedAt": "2026-04-01T12:00:00Z",
            "archived": False,
        }
        rows = [normalize_form({**base, "id": f"f-{1000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"marketing_forms": rows},
            "metadata": {
                "object_type": "marketing_forms",
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
        body = await hubspot_get("/marketing/v3/forms", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"marketing_forms": rows},
                "metadata": {
                    "object_type": "marketing_forms",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        rows.extend(normalize_form(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"marketing_forms": rows},
        "metadata": {
            "object_type": "marketing_forms",
            "mode": "real",
            "row_count": len(rows),
            "pages": pages,
        },
    }
