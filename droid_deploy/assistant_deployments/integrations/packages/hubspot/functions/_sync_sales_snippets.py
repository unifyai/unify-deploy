"""HubSpot sales_snippets sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_sales_snippets(
    schema_version: str = "hubspot.sales.snippets.v1",
    mock: bool = True,
) -> dict:
    """Sync sales snippet definitions.  v0 is mock-only - the snippets API
    requires a beta scope."""
    if mock:
        rows = [
            {
                "snippet_id": f"snip-{1000 + i}",
                "shortcut": "managementfee",
                "text": "Our standard management fee is 7% of monthly rents collected, with a $150 onboarding fee per unit.",
                "created_at": "2025-09-01T10:00:00Z",
                "updated_at": "2025-12-01T12:00:00Z",
            }
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"sales_snippets": rows},
            "metadata": {
                "object_type": "sales_snippets",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    return {
        "schema_version": schema_version,
        "tables": {"sales_snippets": []},
        "metadata": {
            "object_type": "sales_snippets",
            "mode": "real",
            "row_count": 0,
            "note": "Snippets API requires a beta scope; v0 is mock-only.",
        },
    }
