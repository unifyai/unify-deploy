"""Employment Hero account / organisation lookup."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_employmenthero_account_info(mock: bool = True) -> dict:
    """Return identity for the active EH token.

    Mirrors HubSpot's ``get_account_info``.  Resolves the current user
    plus the organisation the assistant is operating against.
    """
    if mock:
        return {
            "user": {
                "id": "user-mock-1",
                "first_name": "Mock",
                "last_name": "Operator",
                "email": "operator@example.test",
            },
            "active_organisation": {
                "id": "org-mock-1",
                "name": "Mock Property Co.",
                "country": "GB",
            },
            "scopes": ["read", "write"],
            "base_url": "https://api.employmenthero.com",
        }

    import os
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
    )

    me = await eh_get("/api/v1/me")
    if "error" in me:
        return me

    active_org_id = os.environ.get("EMPLOYMENTHERO_ORGANISATION_ID")
    active = None
    if active_org_id:
        active = await eh_get(f"/api/v1/organisations/{active_org_id}")
        if "error" in active:
            active = {"id": active_org_id, "_warning": "active org details unavailable"}
    return {
        "user": me.get("data") or me,
        "active_organisation": active,
        "base_url": os.environ.get(
            "EMPLOYMENTHERO_BASE_URL",
            "https://api.employmenthero.com",
        ),
    }


@custom_function()
async def list_employmenthero_organisations(mock: bool = True) -> dict:
    """List organisations the active token can access."""
    if mock:
        return {
            "organisations": [
                {"id": "org-mock-1", "name": "Mock Property Co.", "country": "GB"},
            ],
            "count": 1,
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
    )

    body = await eh_get("/api/v1/organisations")
    if "error" in body:
        return body
    items = body.get("data") or body.get("items") or []
    return {"organisations": items, "count": len(items)}


@custom_function()
async def get_employmenthero_active_organisation(mock: bool = True) -> dict:
    """Resolve and return the active organisation details.

    If ``EMPLOYMENTHERO_ORGANISATION_ID`` is set, fetches that
    organisation's details.  Otherwise returns the first organisation
    the token can access and tells the user to pin it via Console.
    """
    if mock:
        return {
            "id": "org-mock-1",
            "name": "Mock Property Co.",
            "country": "GB",
            "_pinned_via_secret": True,
        }

    import os
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
    )

    pinned = os.environ.get("EMPLOYMENTHERO_ORGANISATION_ID")
    if pinned:
        body = await eh_get(f"/api/v1/organisations/{pinned}")
        if "error" in body:
            return body
        result = body.get("data") or body
        result["_pinned_via_secret"] = True
        return result

    body = await eh_get("/api/v1/organisations")
    if "error" in body:
        return body
    items = body.get("data") or body.get("items") or []
    if not items:
        return {
            "error": (
                "Token has access to no organisations.  Confirm with the "
                "user that the EMPLOYMENTHERO_ACCESS_TOKEN belongs to a "
                "user enrolled in at least one EH organisation."
            ),
        }
    first = items[0]
    first["_pinned_via_secret"] = False
    first["_hint"] = (
        f"Multiple organisations may be available.  Ask the user to set "
        f"EMPLOYMENTHERO_ORGANISATION_ID={first.get('id')} via Console -> "
        f"Secrets to pin this one as the active organisation."
    )
    return first
