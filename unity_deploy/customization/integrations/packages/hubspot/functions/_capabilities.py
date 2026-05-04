"""HubSpot tier / capability detection.

Probes the customer's HubSpot portal to determine which Hubs and
features are available.  Used by tier-gated functions to return a
graceful error rather than raising on a 403.

Underscore-prefixed - not a registered function.
"""

from __future__ import annotations


async def probe_tier() -> dict:
    """Best-effort tier probe.  Returns:

        {
            "portal_id": "12345",
            "currency": "USD",
            "time_zone": "US/Eastern",
            "company_name": "...",
            "features": {
                "sequences": True,
                "workflows": True,
                "sms": False,
                "custom_reports": True,
                "conversations_inbox": True,
                "knowledge_base": True,
                "custom_objects": True,
                "marketing_emails": True,
                "forms": True,
                "campaigns": True,
            },
            "probed_at": "<iso>",
        }

    Returns ``{"error": ...}`` if the token is missing.
    """
    import datetime as _dt

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    info = await hubspot_get("/account-info/v3/details")
    if "error" in info:
        return info

    features: dict[str, bool] = {}
    # Probe each tier-gated surface with a minimal listing call.
    probes = [
        ("sequences", "/automation/v4/sequences", {"limit": 1}),
        ("workflows", "/automation/v4/flows", {"limit": 1}),
        ("custom_reports", "/reports/v1/reports", {"limit": 1}),
        ("conversations_inbox", "/conversations/v3/conversations/inboxes", {"limit": 1}),
        ("knowledge_base", "/cms/v3/knowledge-base/articles", {"limit": 1}),
        ("custom_objects", "/crm/v3/schemas", None),
        ("marketing_emails", "/marketing/v3/emails", {"limit": 1}),
        ("forms", "/marketing/v3/forms", {"limit": 1}),
        ("campaigns", "/marketing/v3/campaigns", {"limit": 1}),
        ("sms", "/marketing/v3/sms/messages", {"limit": 1}),
    ]
    for feature, path, params in probes:
        result = await hubspot_get(path, params=params)
        features[feature] = "error" not in result or result.get("status_code") not in (403, 404)

    return {
        "portal_id": str(info.get("portalId", "")),
        "currency": info.get("companyCurrency", ""),
        "time_zone": info.get("timeZone", ""),
        "company_name": (info.get("additionalCurrencies") or {}),
        "ui_domain": info.get("uiDomain", ""),
        "data_hosting_location": info.get("dataHostingLocation", ""),
        "features": features,
        "probed_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
    }


def gated_error(feature: str, *, hub_required: str = "") -> dict:
    """Construct a graceful error envelope for a tier-gated function."""
    return {
        "error": (
            f"This HubSpot capability ('{feature}') is not enabled for the "
            f"connected portal."
            + (f"  Requires {hub_required}." if hub_required else "")
        ),
        "feature": feature,
        "hub_required": hub_required,
        "upgrade_link": "https://www.hubspot.com/products",
    }
