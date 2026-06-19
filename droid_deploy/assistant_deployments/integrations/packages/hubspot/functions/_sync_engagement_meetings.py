"""HubSpot engagement_meetings sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_meetings(
    since: str | None = None,
    schema_version: str = "hubspot.engagements.meetings.v1",
    mock: bool = True,
) -> dict:
    """Sync meeting engagements modified since ``since`` into a tables envelope."""
    if mock:
        from droid_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_engagement,
        )

        base_props = {
            "hs_meeting_title": "Property Tour - Sunset Tower",
            "hs_meeting_body": "Showed Units 3B and 7A; client preferred 7A.",
            "hs_meeting_location": "Sunset Tower lobby",
            "hs_meeting_outcome": "COMPLETED",
            "hs_meeting_start_time": "2026-04-26T14:00:00Z",
            "hs_meeting_end_time": "2026-04-26T15:00:00Z",
            "hubspot_owner_id": "60001",
            "hs_timestamp": "2026-04-26T14:00:00Z",
        }
        rows = [
            normalize_engagement(
                {
                    "id": f"M{1000 + i}",
                    "properties": {**base_props, "hs_object_id": f"M{1000 + i}"},
                    "createdAt": "2026-04-26T15:30:00Z",
                    "updatedAt": "2026-04-26T15:30:00Z",
                    "archived": False,
                },
                engagement_type="meeting",
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"meetings": rows},
            "metadata": {
                "object_type": "meetings",
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
        "hs_meeting_title",
        "hs_meeting_body",
        "hs_meeting_location",
        "hs_meeting_outcome",
        "hs_meeting_start_time",
        "hs_meeting_end_time",
        "hs_internal_meeting_notes",
        "hubspot_owner_id",
        "hs_timestamp",
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
            "meetings",
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
                "tables": {"meetings": rows},
                "metadata": {
                    "object_type": "meetings",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(
            normalize_engagement(r, engagement_type="meeting")
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
        "tables": {"meetings": rows},
        "metadata": {
            "object_type": "meetings",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
