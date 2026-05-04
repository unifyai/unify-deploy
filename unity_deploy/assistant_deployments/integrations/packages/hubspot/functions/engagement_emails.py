"""HubSpot Engagements - Emails (log a CRM email record + sync)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def log_email(
    subject: str,
    body_text: str,
    direction: str = "EMAIL",
    from_email: str = "",
    to_email: str = "",
    body_html: str | None = None,
    timestamp_iso: str | None = None,
    associations: list[dict] | None = None,
    mock: bool = True,
) -> dict:
    """Log an email engagement (record only; does not send the email)."""
    if mock:
        return {
            "id": "E99001",
            "properties": {
                "hs_email_subject": subject,
                "hs_email_text": body_text,
                "hs_email_html": body_html or body_text,
                "hs_email_direction": direction,
                "hs_email_status": "SENT",
                "hs_email_from_email": from_email,
                "hs_email_to_email": to_email,
                "hs_timestamp": timestamp_iso or "2026-04-23T11:00:00Z",
                "hubspot_owner_id": "60001",
                "hs_object_id": "E99001",
                "hs_lastmodifieddate": "2026-04-23T11:00:00Z",
            },
            "createdAt": "2026-04-23T11:00:00Z",
            "updatedAt": "2026-04-23T11:00:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._engagement_helpers import (
        build_associations,
        now_ms_str,
    )

    body_payload: dict = {
        "properties": {
            "hs_email_subject": subject,
            "hs_email_text": body_text,
            "hs_email_html": body_html or body_text,
            "hs_email_direction": direction,
            "hs_email_from_email": from_email,
            "hs_email_to_email": to_email,
            "hs_timestamp": timestamp_iso or now_ms_str(),
        },
    }
    if associations:
        type_ids = {"contact": 198, "company": 184, "deal": 210, "ticket": 224}
        body_payload["associations"] = build_associations(associations, type_ids)
    return await hubspot_post("/crm/v3/objects/emails", body_payload)


@custom_function()
async def list_emails(
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Paginate through HubSpot email engagements."""
    if mock:
        base_props = {
            "hs_email_subject": "Tour confirmation - Sunset Tower",
            "hs_email_text": "Confirming your tour Saturday at 2pm.",
            "hs_email_direction": "EMAIL",
            "hs_email_status": "SENT",
            "hs_email_from_email": "leasing@example.com",
            "hs_email_to_email": "tenant@example.com",
            "hs_timestamp": "2026-04-23T11:00:00Z",
            "hubspot_owner_id": "60001",
        }
        return {
            "results": [
                {
                    "id": f"E{1000 + i}",
                    "properties": {**base_props, "hs_object_id": f"E{1000 + i}"},
                    "createdAt": "2026-04-23T11:00:00Z",
                    "updatedAt": "2026-04-23T11:00:00Z",
                    "archived": False,
                }
                for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    default_props = [
        "hs_email_subject",
        "hs_email_text",
        "hs_email_html",
        "hs_email_direction",
        "hs_email_status",
        "hs_email_from_email",
        "hs_email_to_email",
        "hs_email_cc_email",
        "hs_timestamp",
        "hubspot_owner_id",
        "hs_object_id",
        "hs_lastmodifieddate",
    ]
    params: dict = {"limit": min(limit, 100), "properties": ",".join(default_props)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/emails", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def sync_emails(
    since: str | None = None,
    schema_version: str = "hubspot.engagements.emails.v1",
    mock: bool = True,
) -> dict:
    """Sync email engagements modified since ``since`` into a tables envelope."""
    if mock:
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_engagement,
        )

        base_props = {
            "hs_email_subject": "Tour confirmation - Sunset Tower",
            "hs_email_text": "Confirming your tour Saturday at 2pm.",
            "hs_email_direction": "EMAIL",
            "hs_email_status": "SENT",
            "hs_email_from_email": "leasing@example.com",
            "hs_email_to_email": "tenant@example.com",
            "hs_timestamp": "2026-04-23T11:00:00Z",
            "hubspot_owner_id": "60001",
        }
        rows = [
            normalize_engagement(
                {
                    "id": f"E{1000 + i}",
                    "properties": {**base_props, "hs_object_id": f"E{1000 + i}"},
                    "createdAt": "2026-04-23T11:00:00Z",
                    "updatedAt": "2026-04-23T11:00:00Z",
                    "archived": False,
                },
                engagement_type="email",
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"emails": rows},
            "metadata": {
                "object_type": "emails",
                "mode": "mock",
                "since": since,
                "row_count": len(rows),
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_engagement,
    )

    default_props = [
        "hs_email_subject",
        "hs_email_text",
        "hs_email_html",
        "hs_email_direction",
        "hs_email_status",
        "hs_email_from_email",
        "hs_email_to_email",
        "hs_email_cc_email",
        "hs_timestamp",
        "hubspot_owner_id",
        "hs_object_id",
        "hs_lastmodifieddate",
    ]
    cfg = get_hubspot_config()
    filter_groups = (
        [
            {
                "filters": [
                    {
                        "propertyName": "hs_lastmodifieddate",
                        "operator": "GT",
                        "value": since,
                    },
                ],
            },
        ]
        if since
        else []
    )
    rows, page_count, after = [], 0, None
    while True:
        body = await hubspot_search(
            "emails",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=default_props,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"emails": rows},
                "metadata": {
                    "object_type": "emails",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(
            normalize_engagement(r, engagement_type="email")
            for r in body.get("results", [])
        )
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (
            cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]
        ):
            break
    return {
        "schema_version": schema_version,
        "tables": {"emails": rows},
        "metadata": {
            "object_type": "emails",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
