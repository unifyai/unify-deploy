"""HubSpot Email Analytics - per-email performance metrics."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_email_performance(
    email_id: str,
    mock: bool = True,
) -> dict:
    """Return marketing email performance metrics: sent, opens, clicks, bounces."""
    if mock:
        return {
            "email_id": str(email_id),
            "metrics": {
                "sent": 1250, "delivered": 1218,
                "open": 487, "open_rate": 0.40,
                "click": 92, "click_rate": 0.075,
                "bounce": 32, "bounce_rate": 0.026,
                "unsubscribe": 4, "unsubscribe_rate": 0.003,
            },
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get(f"/marketing/v3/emails/{email_id}/statistics")
    return body


@custom_function()
async def list_email_event_summary(
    days: int = 7,
    mock: bool = True,
) -> dict:
    """Return rolling-window summary of marketing email events."""
    if mock:
        return {
            "window_days": days,
            "summary": {
                "total_sent": 4800, "delivered": 4710,
                "opens": 1640, "clicks": 312, "bounces": 90,
            },
            "top_emails": [
                {"email_id": "me-2001", "sent": 1250, "open_rate": 0.40},
                {"email_id": "me-2002", "sent": 980, "open_rate": 0.36},
            ],
        }

    return {
        "window_days": days,
        "note": ("Production implementation aggregates from the synced "
                 "HubSpot/Marketing/EmailEvents context; v0 returns mock data only."),
    }
