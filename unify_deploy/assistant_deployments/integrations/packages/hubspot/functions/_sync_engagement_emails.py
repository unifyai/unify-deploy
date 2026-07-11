"""HubSpot engagement_emails sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_emails(
    since: str | None = None,
    schema_version: str = "hubspot.engagements.emails.v1",
    mock: bool = True,
) -> dict:
    """Sync email engagements modified since ``since`` into a tables envelope."""
    if mock:
        from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
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

    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
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
