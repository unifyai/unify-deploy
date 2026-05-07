"""Matterport account / organisation lookup."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_matterport_account_info(mock: bool = True) -> dict:
    """Return identity for the active Matterport credentials.

    Echoes the connected user/org and the current API base URL.  Useful
    as a first-call sanity check after the user pastes credentials.
    """
    if mock:
        return {
            "user": {
                "id": "user-mock-1",
                "email": "operator@example.test",
                "display_name": "Mock Operator",
            },
            "active_organisation": {
                "id": "org-mock-1",
                "name": "Mock Property Co.",
            },
            "base_url": "https://api.matterport.com",
            "tier_hint": "sandbox",
        }

    import os
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )

    body = await matterport_graphql(
        "query { currentUser { id email displayName organizations { id name } } }",
    )
    if isinstance(body, dict) and body.get("error"):
        return body

    user = body.get("currentUser") or {}
    orgs = user.get("organizations") or []
    pinned_id = os.environ.get("MATTERPORT_ORG_ID")
    if pinned_id:
        active = next((o for o in orgs if o.get("id") == pinned_id), None) or {
            "id": pinned_id,
            "_warning": "active org details unavailable",
        }
    else:
        active = orgs[0] if orgs else None

    return {
        "user": {
            "id": user.get("id"),
            "email": user.get("email"),
            "display_name": user.get("displayName"),
        },
        "active_organisation": active,
        "base_url": os.environ.get("MATTERPORT_BASE_URL", "https://api.matterport.com"),
    }


@custom_function()
async def list_matterport_organisations(mock: bool = True) -> dict:
    """List organisations the active credentials can access."""
    if mock:
        return {
            "organisations": [
                {"id": "org-mock-1", "name": "Mock Property Co."},
            ],
            "count": 1,
        }

    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._client import (
        matterport_graphql,
    )

    body = await matterport_graphql(
        "query { currentUser { organizations { id name } } }",
    )
    if isinstance(body, dict) and body.get("error"):
        return body
    items = ((body.get("currentUser") or {}).get("organizations")) or []
    return {"organisations": items, "count": len(items)}
