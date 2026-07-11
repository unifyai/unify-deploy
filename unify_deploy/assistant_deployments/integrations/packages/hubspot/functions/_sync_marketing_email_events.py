"""HubSpot marketing_email_events sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_email_events(
    since_ms: int | None = None,
    schema_version: str = "hubspot.marketing.email_events.v1",
    mock: bool = True,
) -> dict:
    """Sync per-recipient email events into a tables envelope."""
    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "marketing_email_events": [
                    {
                        "event_id": f"ev-{6000 + i}",
                        "type": "OPEN" if i % 2 else "CLICK",
                        "recipient": f"contact{i}@example.com",
                        "email_id": "me-2001",
                        "created_at_ms": 1714060800000 + i * 1000,
                        "filter_pass": True,
                    }
                    for i in range(3)
                ],
            },
            "metadata": {
                "object_type": "marketing_email_events",
                "mode": "mock",
                "since_ms": since_ms,
                "row_count": 3,
            },
        }

    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    cfg = get_hubspot_config()
    rows: list[dict] = []
    pages = 0
    after: str | None = None
    while True:
        params: dict = {"limit": cfg["api_page_size"]}
        if since_ms:
            params["startTimestamp"] = str(since_ms)
        if after:
            params["after"] = after
        body = await hubspot_get("/email/public/v1/events", params=params)
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"marketing_email_events": rows},
                "metadata": {
                    "object_type": "marketing_email_events",
                    "mode": "real",
                    "row_count": len(rows),
                    "pages": pages,
                    "partial": True,
                },
            }
        for ev in body.get("events", []):
            rows.append(
                {
                    "event_id": str(ev.get("id", "")),
                    "type": ev.get("type", ""),
                    "recipient": ev.get("recipient", ""),
                    "email_id": str(ev.get("emailCampaignId", "")),
                    "created_at_ms": ev.get("created"),
                    "filter_pass": True,
                },
            )
        pages += 1
        if not body.get("hasMore"):
            break
        after = body.get("offset")
        if cfg["max_pages_per_sync"] and pages >= cfg["max_pages_per_sync"]:
            break
    return {
        "schema_version": schema_version,
        "tables": {"marketing_email_events": rows},
        "metadata": {
            "object_type": "marketing_email_events",
            "mode": "real",
            "since_ms": since_ms,
            "row_count": len(rows),
            "pages": pages,
        },
    }
