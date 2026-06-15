"""HubSpot Properties - field/property schema metadata + custom property creation."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_hubspot_properties(object_type: str, mock: bool = True) -> dict:
    """List properties (fields) defined on the given object type."""
    if mock:
        return {
            "object_type": object_type,
            "results": [
                {
                    "name": "firstname",
                    "label": "First Name",
                    "type": "string",
                    "fieldType": "text",
                    "groupName": "contactinformation",
                    "hubspotDefined": True,
                    "calculated": False,
                    "hidden": False,
                    "options": [],
                    "description": "",
                },
                {
                    "name": "lastname",
                    "label": "Last Name",
                    "type": "string",
                    "fieldType": "text",
                    "groupName": "contactinformation",
                    "hubspotDefined": True,
                    "calculated": False,
                    "hidden": False,
                    "options": [],
                    "description": "",
                },
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get(f"/crm/v3/properties/{object_type}")
    return (
        body
        if "error" in body
        else {"object_type": object_type, "results": body.get("results", [])}
    )


@custom_function()
async def get_hubspot_property(
    object_type: str,
    property_name: str,
    mock: bool = True,
) -> dict:
    """Fetch a single property definition."""
    if mock:
        return {
            "object_type": object_type,
            "name": property_name,
            "label": property_name.replace("_", " ").title(),
            "type": "string",
            "fieldType": "text",
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(f"/crm/v3/properties/{object_type}/{property_name}")


@custom_function()
async def create_hubspot_property(
    object_type: str,
    name: str,
    label: str,
    field_type: str = "text",
    group_name: str = "contactinformation",
    options: list | None = None,
    mock: bool = True,
) -> dict:
    """Create a custom property on an object type."""
    if mock:
        return {
            "object_type": object_type,
            "name": name,
            "label": label,
            "fieldType": field_type,
            "groupName": group_name,
            "options": options or [],
            "hubspotDefined": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    body: dict = {
        "name": name,
        "label": label,
        "groupName": group_name,
        "type": (
            "enumeration" if field_type in ("select", "checkbox", "radio") else "string"
        ),
        "fieldType": field_type,
    }
    if options:
        body["options"] = options
    return await hubspot_post(f"/crm/v3/properties/{object_type}", body)
