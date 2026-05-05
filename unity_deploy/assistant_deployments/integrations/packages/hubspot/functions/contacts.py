"""HubSpot Contacts - on-demand CRUD + sync.

The canonical CRM object.  Other CRM objects (companies, deals, tickets)
follow this file's structure.

Per integrations/README.md, all module-level constants are kept inside
function bodies so FunctionManager can exec each function in an isolated
namespace.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_hubspot_contact(contact_id: str, mock: bool = True) -> dict:
    """Fetch a single HubSpot contact by ID.  Returns the raw HubSpot record."""
    if mock:
        return {
            "id": str(contact_id),
            "properties": {
                "firstname": "Sample",
                "lastname": "Contact",
                "email": "sample.contact@example.com",
                "phone": "+1 555 123 4567",
                "company": "Acme Properties LLC",
                "jobtitle": "Property Owner",
                "lifecyclestage": "lead",
                "hs_lead_status": "NEW",
                "createdate": "2026-04-01T10:00:00Z",
                "lastmodifieddate": "2026-04-15T14:30:00Z",
                "hs_object_id": str(contact_id),
            },
            "createdAt": "2026-04-01T10:00:00Z",
            "updatedAt": "2026-04-15T14:30:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    default_props = [
        "firstname",
        "lastname",
        "email",
        "phone",
        "company",
        "jobtitle",
        "lifecyclestage",
        "hs_lead_status",
        "createdate",
        "lastmodifieddate",
        "hs_object_id",
    ]
    cfg = get_hubspot_config()
    properties = (
        ",".join(cfg["contact_properties"])
        if isinstance(cfg["contact_properties"], list)
        else ",".join(default_props)
    )
    return await hubspot_get(
        f"/crm/v3/objects/contacts/{contact_id}",
        params={"properties": properties, "archived": "false"},
    )


@custom_function()
async def search_hubspot_contacts(
    query: str, limit: int = 10, mock: bool = True
) -> dict:
    """Full-text search HubSpot contacts."""
    if mock:
        base_props = {
            "firstname": "Sample",
            "lastname": "Contact",
            "email": "sample.contact@example.com",
            "phone": "+1 555 123 4567",
            "company": "Acme Properties LLC",
            "jobtitle": "Property Owner",
            "lifecyclestage": "lead",
            "hs_lead_status": "NEW",
            "createdate": "2026-04-01T10:00:00Z",
            "lastmodifieddate": "2026-04-15T14:30:00Z",
        }
        return {
            "results": [
                {
                    "id": "12345",
                    "properties": {**base_props, "hs_object_id": "12345"},
                    "createdAt": "2026-04-01T10:00:00Z",
                    "updatedAt": "2026-04-15T14:30:00Z",
                    "archived": False,
                },
                {
                    "id": "67890",
                    "properties": {
                        **base_props,
                        "firstname": "Another",
                        "email": "another@example.com",
                        "hs_object_id": "67890",
                    },
                    "createdAt": "2026-04-01T10:00:00Z",
                    "updatedAt": "2026-04-15T14:30:00Z",
                    "archived": False,
                },
            ],
            "total": 2,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_search,
    )

    default_props = [
        "firstname",
        "lastname",
        "email",
        "phone",
        "company",
        "jobtitle",
        "lifecyclestage",
        "hs_lead_status",
        "createdate",
        "lastmodifieddate",
        "hs_object_id",
    ]
    return await hubspot_search(
        "contacts",
        query=query,
        properties=default_props,
        limit=min(limit, 100),
    )


@custom_function()
async def list_hubspot_contacts(
    after: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Paginate through all HubSpot contacts."""
    if mock:
        base_props = {
            "firstname": "Sample",
            "lastname": "Contact",
            "email": "sample.contact@example.com",
            "phone": "+1 555 123 4567",
            "company": "Acme Properties LLC",
            "jobtitle": "Property Owner",
            "lifecyclestage": "lead",
            "hs_lead_status": "NEW",
            "createdate": "2026-04-01T10:00:00Z",
            "lastmodifieddate": "2026-04-15T14:30:00Z",
        }
        return {
            "results": [
                {
                    "id": str(10000 + i),
                    "properties": {**base_props, "hs_object_id": str(10000 + i)},
                    "createdAt": "2026-04-01T10:00:00Z",
                    "updatedAt": "2026-04-15T14:30:00Z",
                    "archived": False,
                }
                for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    default_props = [
        "firstname",
        "lastname",
        "email",
        "phone",
        "company",
        "jobtitle",
        "lifecyclestage",
        "hs_lead_status",
        "createdate",
        "lastmodifieddate",
        "hs_object_id",
    ]
    params: dict = {"limit": min(limit, 100), "properties": ",".join(default_props)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/objects/contacts", params=params)
    if "error" in body:
        return body
    return {
        "results": body.get("results", []),
        "next_after": body.get("paging", {}).get("next", {}).get("after"),
    }


@custom_function()
async def create_hubspot_contact(properties: dict, mock: bool = True) -> dict:
    """Create a HubSpot contact.  ``properties`` is a flat dict of HubSpot
    property names -> values.  At minimum HubSpot expects ``email``."""
    if mock:
        base_props = {
            "firstname": "Sample",
            "lastname": "Contact",
            "email": "sample.contact@example.com",
            "phone": "+1 555 123 4567",
            "company": "Acme Properties LLC",
            "jobtitle": "Property Owner",
            "lifecyclestage": "lead",
            "hs_lead_status": "NEW",
            "createdate": "2026-04-01T10:00:00Z",
            "lastmodifieddate": "2026-04-15T14:30:00Z",
        }
        return {
            "id": "99999",
            "properties": {**base_props, "hs_object_id": "99999", **properties},
            "createdAt": "2026-04-01T10:00:00Z",
            "updatedAt": "2026-04-15T14:30:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        "/crm/v3/objects/contacts",
        {"properties": properties},
    )


@custom_function()
async def update_hubspot_contact(
    contact_id: str, properties: dict, mock: bool = True
) -> dict:
    """Patch HubSpot contact properties.  Only sends the keys provided."""
    if mock:
        base_props = {
            "firstname": "Sample",
            "lastname": "Contact",
            "email": "sample.contact@example.com",
            "phone": "+1 555 123 4567",
            "company": "Acme Properties LLC",
            "jobtitle": "Property Owner",
            "lifecyclestage": "lead",
            "hs_lead_status": "NEW",
            "createdate": "2026-04-01T10:00:00Z",
            "lastmodifieddate": "2026-04-15T14:30:00Z",
        }
        return {
            "id": str(contact_id),
            "properties": {**base_props, "hs_object_id": str(contact_id), **properties},
            "createdAt": "2026-04-01T10:00:00Z",
            "updatedAt": "2026-04-15T14:30:00Z",
            "archived": False,
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_patch,
    )

    return await hubspot_patch(
        f"/crm/v3/objects/contacts/{contact_id}",
        {"properties": properties},
    )


@custom_function()
async def delete_hubspot_contact(contact_id: str, mock: bool = True) -> dict:
    """Soft-delete a HubSpot contact (archive).  Gated by HUBSPOT_ALLOW_DELETE."""
    if mock:
        return {"status": "deleted", "id": str(contact_id)}

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_delete,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_delete"]:
        return {
            "error": "Deletes are disabled.  Set HUBSPOT_ALLOW_DELETE=true on the assistant to enable.",
            "id": str(contact_id),
        }
    return await hubspot_delete(f"/crm/v3/objects/contacts/{contact_id}")


@custom_function()
async def sync_hubspot_contacts(
    since: str | None = None,
    schema_version: str = "hubspot.crm.contacts.v1",
    mock: bool = True,
) -> dict:
    """Pull contacts modified since ``since`` (ISO-8601) into the
    ``{schema_version, tables, metadata}`` envelope used by the scenario
    runtime.  Returns ``tables['contacts']`` flattened for DataManager."""
    if mock:
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._normalize import (
            normalize_contact,
        )

        base_props = {
            "firstname": "Sample",
            "lastname": "Contact",
            "email": "sample.contact@example.com",
            "phone": "+1 555 123 4567",
            "company": "Acme Properties LLC",
            "jobtitle": "Property Owner",
            "lifecyclestage": "lead",
            "hs_lead_status": "NEW",
            "createdate": "2026-04-01T10:00:00Z",
            "lastmodifieddate": "2026-04-15T14:30:00Z",
        }
        rows = [
            normalize_contact(
                {
                    "id": str(10000 + i),
                    "properties": {**base_props, "hs_object_id": str(10000 + i)},
                    "createdAt": "2026-04-01T10:00:00Z",
                    "updatedAt": "2026-04-15T14:30:00Z",
                    "archived": False,
                },
            )
            for i in range(3)
        ]
        return {
            "schema_version": schema_version,
            "tables": {"contacts": rows},
            "metadata": {
                "object_type": "contacts",
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
        normalize_contact,
    )

    default_props = [
        "firstname",
        "lastname",
        "email",
        "phone",
        "company",
        "jobtitle",
        "lifecyclestage",
        "hs_lead_status",
        "createdate",
        "lastmodifieddate",
        "hs_object_id",
    ]
    cfg = get_hubspot_config()
    properties = (
        cfg["contact_properties"]
        if isinstance(cfg["contact_properties"], list)
        else default_props
    )
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

    rows: list[dict] = []
    page_count = 0
    after: str | None = None
    while True:
        body = await hubspot_search(
            "contacts",
            filter_groups=filter_groups,
            sorts=[{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            properties=properties,
            after=after,
            limit=cfg["api_page_size"],
        )
        if "error" in body:
            return {
                "schema_version": schema_version,
                "error": body["error"],
                "tables": {"contacts": rows},
                "metadata": {
                    "object_type": "contacts",
                    "mode": "real",
                    "since": since,
                    "row_count": len(rows),
                    "pages": page_count,
                    "partial": True,
                },
            }
        rows.extend(normalize_contact(r) for r in body.get("results", []))
        page_count += 1
        after = body.get("paging", {}).get("next", {}).get("after")
        if not after or (
            cfg["max_pages_per_sync"] and page_count >= cfg["max_pages_per_sync"]
        ):
            break

    return {
        "schema_version": schema_version,
        "tables": {"contacts": rows},
        "metadata": {
            "object_type": "contacts",
            "mode": "real",
            "since": since,
            "row_count": len(rows),
            "pages": page_count,
        },
    }
