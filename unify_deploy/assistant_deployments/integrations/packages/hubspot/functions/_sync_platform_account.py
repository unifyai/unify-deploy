"""HubSpot platform_account sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_hubspot_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_hubspot_platform(
    schema_version: str = "hubspot.platform.v1",
    mock: bool = True,
) -> dict:
    """Sync platform-level metadata: account info, currencies, business
    units.  Audit logs and imports are higher-volume; opt-in via separate
    config flags."""
    if mock:
        account_rows = [
            {
                "portal_id": "12345",
                "ui_domain": "app.hubspot.com",
                "data_hosting_location": "NA",
                "time_zone": "US/Eastern",
                "company_currency": "USD",
            },
        ]
        currency_rows = [
            {"code": "USD", "exchange_rate": 1.0, "active": True},
            {"code": "EUR", "exchange_rate": 0.92, "active": True},
        ]
        bu_rows = [
            {"unit_id": "bu-1", "name": "ClientZeta Multifamily", "is_primary": True},
        ]
        return {
            "schema_version": schema_version,
            "tables": {
                "account_info": account_rows,
                "currencies": currency_rows,
                "business_units": bu_rows,
            },
            "metadata": {"object_type": "platform", "mode": "mock"},
        }

    from unify_deploy.assistant_deployments.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    account_body = await hubspot_get("/account-info/v3/details")
    cur_body = await hubspot_get("/account-info/v3/currencies")
    bu_body = await hubspot_get("/business-units/v3/business-units/user/me")

    account_rows: list[dict] = []
    if "error" not in account_body:
        account_rows = [
            {
                "portal_id": str(account_body.get("portalId", "")),
                "ui_domain": account_body.get("uiDomain", ""),
                "data_hosting_location": account_body.get("dataHostingLocation", ""),
                "time_zone": account_body.get("timeZone", ""),
                "company_currency": account_body.get("companyCurrency", ""),
            },
        ]

    currency_rows: list[dict] = []
    if "error" not in cur_body:
        for c in cur_body.get("results", []):
            currency_rows.append(
                {
                    "code": c.get("code", ""),
                    "exchange_rate": c.get("exchangeRate"),
                    "active": bool(c.get("active", True)),
                },
            )

    bu_rows: list[dict] = []
    if "error" not in bu_body:
        bus = bu_body.get("results", []) if isinstance(bu_body, dict) else []
        for b in bus:
            bu_rows.append(
                {
                    "unit_id": str(b.get("id", "")),
                    "name": b.get("name", ""),
                    "is_primary": bool(b.get("isPrimary", False)),
                },
            )

    return {
        "schema_version": schema_version,
        "tables": {
            "account_info": account_rows,
            "currencies": currency_rows,
            "business_units": bu_rows,
        },
        "metadata": {
            "object_type": "platform",
            "mode": "real",
            "rows": {
                "account_info": len(account_rows),
                "currencies": len(currency_rows),
                "business_units": len(bu_rows),
            },
        },
    }
