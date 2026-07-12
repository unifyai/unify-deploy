"""HubSpot associations sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_associations(
    schema_version: str = "hubspot.crm.associations.v1",
    mock: bool = True,
) -> dict:
    """Sync canonical association pairs into one flat table.

    HubSpot's batch-read endpoints only accept a list of source IDs, so a
    full association sync requires iterating from-side IDs.  This v0
    implementation pulls a sample to seed the schema; production should
    drive this from the synced dimension tables in DataManager (out of
    scope for v0)."""
    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
        normalize_association,
    )

    association_pairs = [
        ("contacts", "companies"),
        ("contacts", "deals"),
        ("contacts", "tickets"),
        ("companies", "deals"),
        ("companies", "tickets"),
        ("deals", "line_items"),
        ("deals", "quotes"),
    ]

    if mock:
        rows = [
            normalize_association(
                from_id="12345",
                to_id="5001",
                association_type="contact_to_company",
                from_object_type="contacts",
                to_object_type="companies",
            ),
            normalize_association(
                from_id="12345",
                to_id="9001",
                association_type="contact_to_deal",
                from_object_type="contacts",
                to_object_type="deals",
            ),
            normalize_association(
                from_id="9001",
                to_id="5001",
                association_type="deal_to_company",
                from_object_type="deals",
                to_object_type="companies",
            ),
            normalize_association(
                from_id="9001",
                to_id="7001",
                association_type="deal_to_line_item",
                from_object_type="deals",
                to_object_type="line_items",
            ),
        ]
        return {
            "schema_version": schema_version,
            "tables": {"associations": rows},
            "metadata": {
                "object_type": "associations",
                "mode": "mock",
                "row_count": len(rows),
            },
        }

    return {
        "schema_version": schema_version,
        "tables": {"associations": []},
        "metadata": {
            "object_type": "associations",
            "mode": "real",
            "row_count": 0,
            "note": (
                "v0 association sync seeds nothing; production "
                "iteration drives this from the synced dimension tables."
            ),
            "pairs": [{"from": a, "to": b} for a, b in association_pairs],
        },
    }
