"""HubSpot Associations - links between CRM records (v4 API)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_associations(
    from_object_type: str,
    from_id: str,
    to_object_type: str,
    mock: bool = True,
) -> dict:
    """List associations from one record to all records of another type."""
    if mock:
        return {
            "from_object_type": from_object_type,
            "from_id": str(from_id),
            "to_object_type": to_object_type,
            "results": [
                {"toObjectId": "5001",
                 "associationTypes": [{"category": "HUBSPOT_DEFINED",
                                       "typeId": 1, "label": None}]},
            ],
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(
        f"/crm/v4/objects/{from_object_type}/{from_id}/associations/{to_object_type}",
    )


@custom_function()
async def create_association(
    from_object_type: str,
    from_id: str,
    to_object_type: str,
    to_id: str,
    association_type_id: int = 1,
    mock: bool = True,
) -> dict:
    """Create an association between two records.  Defaults to the
    HubSpot-defined primary association for the pair (typeId=1)."""
    if mock:
        return {"status": "associated",
                "from": {"object_type": from_object_type, "id": str(from_id)},
                "to": {"object_type": to_object_type, "id": str(to_id)},
                "association_type_id": association_type_id}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v4/objects/{from_object_type}/{from_id}"
        f"/associations/default/{to_object_type}/{to_id}",
        {},
    )


@custom_function()
async def delete_association(
    from_object_type: str,
    from_id: str,
    to_object_type: str,
    to_id: str,
    mock: bool = True,
) -> dict:
    """Delete an association between two records."""
    if mock:
        return {"status": "deleted",
                "from": {"object_type": from_object_type, "id": str(from_id)},
                "to": {"object_type": to_object_type, "id": str(to_id)}}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )

    return await hubspot_delete(
        f"/crm/v4/objects/{from_object_type}/{from_id}"
        f"/associations/{to_object_type}/{to_id}",
    )


@custom_function()
async def sync_associations(
    schema_version: str = "hubspot.crm.associations.v1",
    mock: bool = True,
) -> dict:
    """Sync canonical association pairs into one flat table.

    HubSpot's batch-read endpoints only accept a list of source IDs, so a
    full association sync requires iterating from-side IDs.  This v0
    implementation pulls a sample to seed the schema; production should
    drive this from the synced dimension tables in DataManager (out of
    scope for v0)."""
    from unity_deploy.customization.integrations.packages.hubspot.functions._normalize import (
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
                from_id="12345", to_id="5001", association_type="contact_to_company",
                from_object_type="contacts", to_object_type="companies",
            ),
            normalize_association(
                from_id="12345", to_id="9001", association_type="contact_to_deal",
                from_object_type="contacts", to_object_type="deals",
            ),
            normalize_association(
                from_id="9001", to_id="5001", association_type="deal_to_company",
                from_object_type="deals", to_object_type="companies",
            ),
            normalize_association(
                from_id="9001", to_id="7001", association_type="deal_to_line_item",
                from_object_type="deals", to_object_type="line_items",
            ),
        ]
        return {
            "schema_version": schema_version,
            "tables": {"associations": rows},
            "metadata": {"object_type": "associations", "mode": "mock",
                         "row_count": len(rows)},
        }

    return {
        "schema_version": schema_version,
        "tables": {"associations": []},
        "metadata": {
            "object_type": "associations", "mode": "real",
            "row_count": 0,
            "note": ("v0 association sync seeds nothing; production "
                     "iteration drives this from the synced dimension tables."),
            "pairs": [{"from": a, "to": b} for a, b in association_pairs],
        },
    }
