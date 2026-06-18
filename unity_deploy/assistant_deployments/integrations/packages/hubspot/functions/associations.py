"""HubSpot Associations - links between CRM records (v4 API)."""

from __future__ import annotations

from droid.function_manager.custom import custom_function


@custom_function()
async def list_hubspot_associations(
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
                {
                    "toObjectId": "5001",
                    "associationTypes": [
                        {
                            "category": "HUBSPOT_DEFINED",
                            "typeId": 1,
                            "label": None,
                        },
                    ],
                },
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(
        f"/crm/v4/objects/{from_object_type}/{from_id}/associations/{to_object_type}",
    )


@custom_function()
async def create_hubspot_association(
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
        return {
            "status": "associated",
            "from": {"object_type": from_object_type, "id": str(from_id)},
            "to": {"object_type": to_object_type, "id": str(to_id)},
            "association_type_id": association_type_id,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v4/objects/{from_object_type}/{from_id}"
        f"/associations/default/{to_object_type}/{to_id}",
        {},
    )


@custom_function()
async def delete_hubspot_association(
    from_object_type: str,
    from_id: str,
    to_object_type: str,
    to_id: str,
    mock: bool = True,
) -> dict:
    """Delete an association between two records."""
    if mock:
        return {
            "status": "deleted",
            "from": {"object_type": from_object_type, "id": str(from_id)},
            "to": {"object_type": to_object_type, "id": str(to_id)},
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )

    return await hubspot_delete(
        f"/crm/v4/objects/{from_object_type}/{from_id}"
        f"/associations/{to_object_type}/{to_id}",
    )
