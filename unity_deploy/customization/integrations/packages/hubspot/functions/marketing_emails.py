"""HubSpot Marketing Emails - definitions, single-send, broadcast (HIGH-STAKES)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_marketing_emails(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    """Paginate through marketing email definitions."""
    if mock:
        base = {
            "name": "Owner Newsletter - April 2026",
            "subject": "Q1 portfolio update + spring projects",
            "fromName": "ClientZeta Properties",
            "state": "PUBLISHED",
            "createdAt": "2026-04-01T10:00:00Z",
            "updatedAt": "2026-04-15T12:00:00Z",
            "publishDate": "2026-04-15T13:00:00Z",
        }
        return {
            "results": [{**base, "id": f"me-{2000 + i}"} for i in range(min(limit, 3))],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/marketing/v3/emails", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def get_marketing_email(email_id: str, mock: bool = True) -> dict:
    """Fetch a marketing email by ID."""
    if mock:
        return {
            "id": str(email_id),
            "name": "Owner Newsletter - April 2026",
            "subject": "Q1 portfolio update + spring projects",
            "fromName": "ClientZeta Properties",
            "state": "PUBLISHED",
            "createdAt": "2026-04-01T10:00:00Z",
            "updatedAt": "2026-04-15T12:00:00Z",
            "publishDate": "2026-04-15T13:00:00Z",
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/marketing/v3/emails/{email_id}")


@custom_function()
async def send_single_marketing_email(
    email_id: str,
    contact_email: str,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Send a transactional / single-send marketing email to one contact."""
    if not confirm:
        return {
            "error": "send_single_marketing_email requires confirm=True.  "
                     "Confirm with the user that the email and recipient are correct.",
            "email_id": str(email_id), "contact_email": contact_email,
        }
    if mock:
        return {"status": "queued", "email_id": str(email_id),
                "contact_email": contact_email, "mock": True}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        "/marketing/v3/transactional/single-email/send",
        {"emailId": email_id, "message": {"to": contact_email}},
    )


@custom_function()
async def broadcast_marketing_email(
    email_id: str,
    list_id: str,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """HIGH-STAKES: send a marketing email to every contact in a list.

    Requires both ``confirm=True`` AND HUBSPOT_ALLOW_BROADCAST_EMAIL=true."""
    if not confirm:
        return {
            "error": "broadcast_marketing_email requires confirm=True.  Confirm "
                     "with the user that the recipient list and content are correct.",
            "email_id": str(email_id), "list_id": str(list_id),
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_broadcast_email"]:
        return {
            "error": "Broadcast email sends are disabled by config.  "
                     "Set HUBSPOT_ALLOW_BROADCAST_EMAIL=true on the assistant to enable.",
            "email_id": str(email_id), "list_id": str(list_id),
        }

    if mock:
        return {"status": "scheduled", "email_id": str(email_id),
                "list_id": str(list_id), "mock": True}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        f"/marketing/v3/emails/{email_id}/blast-send",
        {"listIds": [str(list_id)]},
    )


@custom_function()
async def sync_marketing_emails(
    schema_version: str = "hubspot.marketing.emails.v1",
    mock: bool = True,
) -> dict:
    """Sync marketing email definitions into a tables envelope."""
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
        normalize_marketing_email,
    )

    if mock:
        base = {
            "name": "Owner Newsletter - April 2026",
            "subject": "Q1 portfolio update + spring projects",
            "fromName": "ClientZeta Properties",
            "state": "PUBLISHED",
            "createdAt": "2026-04-01T10:00:00Z",
            "updatedAt": "2026-04-15T12:00:00Z",
            "publishDate": "2026-04-15T13:00:00Z",
        }
        rows = [normalize_marketing_email({**base, "id": f"me-{2000 + i}"}) for i in range(3)]
        return {
            "schema_version": schema_version,
            "tables": {"marketing_emails": rows},
            "metadata": {"object_type": "marketing_emails", "mode": "mock",
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
        body = await hubspot_get("/marketing/v3/emails", params=params)
        if "error" in body:
            return {"schema_version": schema_version, "error": body["error"],
                    "tables": {"marketing_emails": rows},
                    "metadata": {"object_type": "marketing_emails", "mode": "real",
                                 "row_count": len(rows), "pages": pages, "partial": True}}
        rows.extend(normalize_marketing_email(r) for r in body.get("results", []))
        pages += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return {
        "schema_version": schema_version,
        "tables": {"marketing_emails": rows},
        "metadata": {"object_type": "marketing_emails", "mode": "real",
                     "row_count": len(rows), "pages": pages},
    }
