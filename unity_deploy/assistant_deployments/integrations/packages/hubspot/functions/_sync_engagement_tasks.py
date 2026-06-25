"""HubSpot engagement_tasks sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_tasks(
    since: str | None = None,
    schema_version: str = "hubspot.engagements.tasks.v1",
    mock: bool = True,
) -> dict:
    """Sync tasks modified since ``since`` into a tables envelope."""
    if mock:
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_engagement,
        )

        base_props = {
            "hs_task_subject": "Follow up with prospective tenant",
            "hs_task_body": "Send unit availability for August move-in.",
            "hs_task_status": "NOT_STARTED",
            "hs_task_priority": "MEDIUM",
            "hs_task_type": "TODO",
            "hs_timestamp": "2026-04-28T09:00:00Z",
            "hubspot_owner_id": "60001",
        }
        rows = [
            normalize_engagement(
                {
                    "id": f"T{1000 + i}",
                    "properties": {**base_props, "hs_object_id": f"T{1000 + i}"},
                    "createdAt": "2026-04-26T16:00:00Z",
                    "updatedAt": "2026-04-26T16:00:00Z",
                    "archived": False,
                },
                engagement_type="task",
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"tasks": rows},
            "metadata": {
                "object_type": "tasks",
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
        "hs_task_subject",
        "hs_task_body",
        "hs_task_status",
        "hs_task_priority",
        "hs_task_type",
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
            "tasks",
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
                "tables": {"tasks": rows},
                "metadata": {
                    "object_type": "tasks",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(
            normalize_engagement(r, engagement_type="task")
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
        "tables": {"tasks": rows},
        "metadata": {
            "object_type": "tasks",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
