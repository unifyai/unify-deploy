"""HubSpot Marketing Forms - definitions, submissions, programmatic submit."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_FORM = {
    "id": "f-1001",
    "name": "Property Inquiry",
    "formType": "hubspot",
    "createdAt": "2025-10-01T10:00:00Z",
    "updatedAt": "2026-04-01T12:00:00Z",
    "archived": False,
}

_MOCK_SUBMISSION = {
    "submittedAt": "2026-04-25T14:00:00Z",
    "values": [
        {"name": "email", "value": "tenant@example.com"},
        {"name": "firstname", "value": "Jane"},
        {"name": "message", "value": "Interested in 2-bedroom unit at Sunset Tower."},
    ],
    "pageUrl": "https://example.com/properties/sunset-tower",
}


@custom_function()
async def list_marketing_forms(after: str | None = None, limit: int = 50, mock: bool = True) -> dict:
    if mock:
        return {"results": [{**_MOCK_FORM, "id": f"f-{1000 + i}"} for i in range(min(limit, 3))],
                "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/marketing/v3/forms", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_marketing_form(form_id: str, mock: bool = True) -> dict:
    if mock:
        return {**_MOCK_FORM, "id": str(form_id)}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/marketing/v3/forms/{form_id}")


@custom_function()
async def list_form_submissions(
    form_id: str,
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    if mock:
        return {"results": [_MOCK_SUBMISSION] * min(limit, 3), "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 50)}
    if after:
        params["after"] = after
    body = await hubspot_get(f"/form-integrations/v1/submissions/forms/{form_id}", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def submit_form(
    portal_id: str,
    form_id: str,
    fields: dict,
    page_uri: str = "",
    mock: bool = True,
) -> dict:
    """Programmatically submit a HubSpot form (no auth required - uses
    the public submission endpoint)."""
    if mock:
        return {"status": "ok", "form_id": form_id, "fields": fields, "mock": True}

    import httpx

    body = {
        "fields": [{"name": k, "value": v} for k, v in fields.items()],
        "context": {"pageUri": page_uri},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.hsforms.com/submissions/v3/integration/submit/{portal_id}/{form_id}",
            json=body,
        )
    return resp.json() if resp.text else {"status": "ok", "status_code": resp.status_code}


@custom_function()
async def sync_marketing_forms(
    schema_version: str = "hubspot.marketing.forms.v1",
    mock: bool = True,
) -> dict:
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_form,
    )

    if mock:
        rows = [normalize_form({**_MOCK_FORM, "id": f"f-{1000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"marketing_forms": rows},
            "metadata": {"object_type": "marketing_forms", "mode": "mock",
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
        body = await hubspot_get("/marketing/v3/forms", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"marketing_forms": rows},
                    "metadata": {"object_type": "marketing_forms", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        rows.extend(normalize_form(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"marketing_forms": rows},
        "metadata": {"object_type": "marketing_forms", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
