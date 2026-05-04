"""HubSpot Marketing SMS (tier-gated, HIGH-STAKES)."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def send_marketing_sms(
    contact_phone: str,
    message: str,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Send a marketing SMS to a contact.

    HIGH-STAKES: requires both ``confirm=True`` AND
    HUBSPOT_ALLOW_BROADCAST_SMS=true.  Tier-gated (Marketing Hub Pro+ with
    SMS add-on)."""
    if not confirm:
        return {
            "error": "send_marketing_sms requires confirm=True.  Confirm "
                     "with the user that the message and recipient are correct.",
            "phone": contact_phone,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    if not get_hubspot_config()["allow_broadcast_sms"]:
        return {
            "error": "Broadcast SMS is disabled by config.  "
                     "Set HUBSPOT_ALLOW_BROADCAST_SMS=true on the assistant to enable.",
            "phone": contact_phone,
        }

    if mock:
        return {"status": "queued", "phone": contact_phone, "message": message[:100], "mock": True}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_post,
    )

    return await hubspot_post(
        "/marketing/v3/sms/messages",
        {"recipient": {"phoneNumber": contact_phone}, "message": message},
    )


@custom_function()
async def list_sms_messages(
    after: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    """List SMS message records (tier-gated)."""
    if mock:
        return {
            "results": [
                {
                    "id": f"sms-{8000 + i}", "phoneNumber": "+1 555 123 4567",
                    "status": "DELIVERED", "createdAt": "2026-04-25T15:00:00Z",
                }
                for i in range(min(limit, 3))
            ],
            "next_after": None,
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/marketing/v3/sms/messages", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}
