"""HubSpot marketing_subscriptions sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_subscriptions(
    schema_version: str = "hubspot.marketing.subscriptions.v1",
    mock: bool = True,
) -> dict:
    """Sync subscription type definitions.  Per-contact status is fetched
    on-demand (too expensive to bulk-pull)."""
    if mock:
        base = {
            "name": "Owner Updates",
            "description": "Quarterly portfolio summary for property owners.",
            "active": True,
        }
        rows = [
            {
                "subscription_id": f"sub-{5000 + i}",
                "name": base["name"],
                "description": base["description"],
                "active": base["active"],
            }
            for i in range(2)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"subscription_types": rows, "contact_subscriptions": []},
            "metadata": {
                "object_type": "subscription_types",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get("/communication-preferences/v3/definitions")
    if "error" in body:
        return {
            "schema_version": schema_version,
            "error": body["error"],
            "tables": {"subscription_types": [], "contact_subscriptions": []},
            "metadata": {
                "object_type": "subscription_types",
                "mode": "real",
                "row_count": 0,
                "partial": True,
            },
        }

    rows = []
    for r in body.get("subscriptionDefinitions", []):
        rows.append(
            {
                "subscription_id": str(r.get("id", "")),
                "name": r.get("name", ""),
                "description": r.get("description", ""),
                "active": bool(r.get("active", True)),
            },
        )
    return {
        "schema_version": schema_version,
        "tables": {"subscription_types": rows, "contact_subscriptions": []},
        "metadata": {
            "object_type": "subscription_types",
            "mode": "real",
            "row_count": len(rows),
        },
    }
