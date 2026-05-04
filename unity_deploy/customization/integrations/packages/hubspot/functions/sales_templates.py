"""HubSpot Sales Email Templates (read-only)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_TEMPLATE = {
    "id": "tpl-9501",
    "name": "Initial Owner Outreach",
    "subject": "Quick question about {{company.name}}",
    "body": "Hi {{contact.firstname}},\n\nI noticed your portfolio includes ...",
    "createdAt": "2025-10-01T10:00:00Z",
    "updatedAt": "2026-02-15T12:00:00Z",
}


@custom_function()
async def list_sales_templates(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    if mock:
        return {"results": [{**_MOCK_TEMPLATE, "id": f"tpl-{9500 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/marketing/v3/transactional/templates", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_sales_template(template_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_TEMPLATE, "id": str(template_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/marketing/v3/transactional/templates/{template_id}")


@custom_function()
async def sync_sales_templates(
    schema_version: str = "hubspot.sales.templates.v1",
    mock: bool = True,
) -> dict:
    if mock:
        rows = [{
            "template_id": f"tpl-{9500 + i}",
            "name": _MOCK_TEMPLATE["name"], "subject": _MOCK_TEMPLATE["subject"],
            "body_preview": _MOCK_TEMPLATE["body"][:200],
            "created_at": _MOCK_TEMPLATE["createdAt"],
            "updated_at": _MOCK_TEMPLATE["updatedAt"],
        } for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"sales_templates": rows},
            "metadata": {"object_type": "sales_templates", "mode": "mock",
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
        body = await hubspot_get("/marketing/v3/transactional/templates", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"sales_templates": rows},
                    "metadata": {"object_type": "sales_templates", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        for r in body.get("results", []):
            rows.append({
                "template_id": str(r.get("id", "")),
                "name": r.get("name", ""), "subject": r.get("subject", ""),
                "body_preview": (r.get("body") or "")[:200],
                "created_at": r.get("createdAt", ""),
                "updated_at": r.get("updatedAt", ""),
            })
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"sales_templates": rows},
        "metadata": {"object_type": "sales_templates", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
