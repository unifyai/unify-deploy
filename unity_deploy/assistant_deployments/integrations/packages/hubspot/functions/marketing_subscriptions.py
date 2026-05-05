"""HubSpot Subscription Preferences - types + per-contact status."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_hubspot_subscription_types(mock: bool = True) -> dict:
    """List all subscription types defined in the portal."""
    if mock:
        base = {
            "name": "Owner Updates",
            "description": "Quarterly portfolio summary for property owners.",
            "active": True,
        }
        return {
            "results": [
                {**base, "id": "sub-5001"},
                {**base, "id": "sub-5002", "name": "Tenant Newsletter"},
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get("/communication-preferences/v3/definitions")
    if "error" in body:
        return body
    return {"results": body.get("subscriptionDefinitions", [])}


@custom_function()
async def get_hubspot_contact_subscription_status(
    contact_email: str,
    mock: bool = True,
) -> dict:
    """Return the contact's status across all subscription types."""
    if mock:
        return {
            "email": contact_email,
            "subscriptions": [
                {"id": "sub-5001", "status": "SUBSCRIBED"},
                {"id": "sub-5002", "status": "NOT_OPTED"},
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get(
        f"/communication-preferences/v3/status/email/{contact_email}",
    )


@custom_function()
async def subscribe_hubspot_contact(
    contact_email: str,
    subscription_id: str,
    legal_basis: str = "LEGITIMATE_INTEREST_PQL",
    legal_basis_explanation: str = "Customer requested updates.",
    mock: bool = True,
) -> dict:
    """Subscribe a contact to a subscription type."""
    if mock:
        return {
            "status": "subscribed",
            "email": contact_email,
            "subscription_id": str(subscription_id),
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        "/communication-preferences/v3/subscribe",
        {
            "emailAddress": contact_email,
            "subscriptionId": subscription_id,
            "legalBasis": legal_basis,
            "legalBasisExplanation": legal_basis_explanation,
        },
    )


@custom_function()
async def unsubscribe_hubspot_contact(
    contact_email: str,
    subscription_id: str,
    mock: bool = True,
) -> dict:
    """Unsubscribe a contact from a subscription type."""
    if mock:
        return {
            "status": "unsubscribed",
            "email": contact_email,
            "subscription_id": str(subscription_id),
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        "/communication-preferences/v3/unsubscribe",
        {"emailAddress": contact_email, "subscriptionId": subscription_id},
    )


@custom_function()
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

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
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
