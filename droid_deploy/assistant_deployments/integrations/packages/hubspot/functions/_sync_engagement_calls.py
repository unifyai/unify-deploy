"""HubSpot engagement_calls sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_calls(
    since: str | None = None,
    schema_version: str = "hubspot.engagements.calls.v1",
    mock: bool = True,
) -> dict:
    """Sync call engagements modified since ``since`` into a tables envelope."""
    if mock:
        from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_engagement,
        )

        base_props = {
            "hs_call_title": "Discovery call - prospective tenant",
            "hs_call_body": "Discussed move-in date, unit preferences, budget.",
            "hs_call_direction": "OUTBOUND",
            "hs_call_duration": "1200",
            "hs_call_status": "COMPLETED",
            "hs_timestamp": "2026-04-25T14:00:00Z",
            "hubspot_owner_id": "60001",
        }
        rows = [
            normalize_engagement(
                {
                    "id": f"C{1000 + i}",
                    "properties": {**base_props, "hs_object_id": f"C{1000 + i}"},
                    "createdAt": "2026-04-25T14:30:00Z",
                    "updatedAt": "2026-04-25T14:30:00Z",
                    "archived": False,
                },
                engagement_type="call",
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"calls": rows},
            "metadata": {
                "object_type": "calls",
                "mode": "mock",
                "since": since,
                "row_count": len(rows),
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_engagement,
    )

    default_props = [
        "hs_call_title",
        "hs_call_body",
        "hs_call_direction",
        "hs_call_duration",
        "hs_call_from_number",
        "hs_call_to_number",
        "hs_call_status",
        "hs_call_disposition",
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
            "calls",
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
                "tables": {"calls": rows},
                "metadata": {
                    "object_type": "calls",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(
            normalize_engagement(r, engagement_type="call")
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
        "tables": {"calls": rows},
        "metadata": {
            "object_type": "calls",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
