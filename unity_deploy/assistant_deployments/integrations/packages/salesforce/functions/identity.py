"""Token / connection check for the Salesforce package.

``get_salesforce_me`` confirms the OAuth credentials work end-to-end:
it mints an access token (using the cached one if still valid) and
hits the OAuth ``/services/oauth2/userinfo`` endpoint, which returns
the connected user's identity + the org id without requiring any
sObject permission.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
async def get_salesforce_me(mock: bool = True) -> dict:
    """Identify the connected Salesforce user.

    Wraps ``GET /services/oauth2/userinfo``, which is reachable with
    just the ``api`` (or even ``id``) scope and does not require any
    sObject permission.  Use this as the first call after Connect to
    confirm the credentials are wired up correctly.
    """
    if mock:
        return {
            "user_id": "005000000000001",
            "organization_id": "00D000000000001",
            "username": "alex@example.test.acme",
            "display_name": "Alex Example",
            "email": "alex@example.test",
            "preferred_username": "alex@example.test.acme",
            "_mock": True,
        }

    from unity_deploy.assistant_deployments.integrations.packages.salesforce.functions._client import (
        salesforce_get,
    )

    body = await salesforce_get("/services/oauth2/userinfo")
    if "error" in body:
        return body
    # Userinfo keys are already snake-case-ish; normalise the few that
    # vary between SF environments.
    return {
        "user_id": body.get("user_id"),
        "organization_id": body.get("organization_id"),
        "username": body.get("preferred_username") or body.get("username"),
        "display_name": body.get("name"),
        "email": body.get("email"),
        "preferred_username": body.get("preferred_username"),
        "raw": body,
    }
