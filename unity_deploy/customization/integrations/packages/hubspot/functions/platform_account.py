"""HubSpot Platform - account info, audit logs, currencies, business units, imports."""

from __future__ import annotations

from unity.function_manager.custom import custom_function

_MOCK_ACCOUNT = {
    "portalId": 12345,
    "uiDomain": "app.hubspot.com",
    "dataHostingLocation": "NA",
    "timeZone": "US/Eastern",
    "companyCurrency": "USD",
    "additionalCurrencies": [],
}


@custom_function()
async def get_account_info(mock: bool = True) -> dict:
    """Fetch HubSpot account details.  Bootstraps HUBSPOT_PORTAL_ID and tier
    detection in one call."""
    if mock:
        return _MOCK_ACCOUNT

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    return await hubspot_get("/account-info/v3/details")


@custom_function()
async def list_audit_logs(
    after: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """List recent audit log entries (read-only)."""
    if mock:
        return {
            "results": [
                {"id": f"al-{14000 + i}", "objectId": "12345", "objectType": "CONTACT",
                 "action": "UPDATED", "userId": "60001",
                 "occurredAt": "2026-04-26T15:00:00Z"}
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
    body = await hubspot_get("/account-info/v3/audit-logs", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def list_currencies(mock: bool = True) -> dict:
    """List active currencies + exchange rates."""
    if mock:
        return {"results": [
            {"code": "USD", "exchangeRate": 1.0, "active": True},
            {"code": "EUR", "exchangeRate": 0.92, "active": True},
        ]}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get("/account-info/v3/currencies")
    if "error" in body:
        return body
    return {"results": body.get("results", [])}


@custom_function()
async def list_business_units(mock: bool = True) -> dict:
    """List business units (multi-brand setups)."""
    if mock:
        return {"results": [
            {"id": "bu-1", "name": "ClientZeta Multifamily", "isPrimary": True},
        ]}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    body = await hubspot_get("/business-units/v3/business-units/user/me")
    if "error" in body:
        return body
    return {"results": body.get("results", []) if isinstance(body, dict) else []}


@custom_function()
async def list_imports(after: str | None = None, limit: int = 50, mock: bool = True) -> dict:
    if mock:
        return {"results": [
            {"id": f"imp-{15000 + i}", "name": f"Import {i}",
             "state": "DONE", "createdAt": "2026-04-01T10:00:00Z"}
            for i in range(min(limit, 3))
        ], "next_after": None}

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    params: dict = {"limit": min(limit, 100)}
    if after:
        params["after"] = after
    body = await hubspot_get("/crm/v3/imports", params=params)
    if "error" in body:
        return body
    return {"results": body.get("results", []),
            "next_after": body.get("paging", {}).get("next", {}).get("after")}


@custom_function()
async def sync_platform(
    schema_version: str = "hubspot.platform.v1",
    mock: bool = True,
) -> dict:
    """Sync platform-level metadata: account info, currencies, business
    units.  Audit logs and imports are higher-volume; opt-in via separate
    config flags."""
    if mock:
        account_rows = [{
            "portal_id": str(_MOCK_ACCOUNT["portalId"]),
            "ui_domain": _MOCK_ACCOUNT["uiDomain"],
            "data_hosting_location": _MOCK_ACCOUNT["dataHostingLocation"],
            "time_zone": _MOCK_ACCOUNT["timeZone"],
            "company_currency": _MOCK_ACCOUNT["companyCurrency"],
        }]
        currency_rows = [
            {"code": "USD", "exchange_rate": 1.0, "active": True},
            {"code": "EUR", "exchange_rate": 0.92, "active": True},
        ]
        bu_rows = [{"unit_id": "bu-1", "name": "ClientZeta Multifamily", "is_primary": True}]
        return {
            "schema_version": schema_version,
            "tables": {
                "account_info": account_rows,
                "currencies": currency_rows,
                "business_units": bu_rows,
            },
            "metadata": {"object_type": "platform", "mode": "mock"},
        }

    from unity_deploy.customization.integrations.packages.hubspot.functions._client import (
        hubspot_get,
    )

    account_body = await hubspot_get("/account-info/v3/details")
    cur_body = await hubspot_get("/account-info/v3/currencies")
    bu_body = await hubspot_get("/business-units/v3/business-units/user/me")

    account_rows: list[dict] = []
    if "error" not in account_body:
        account_rows = [{
            "portal_id": str(account_body.get("portalId", "")),
            "ui_domain": account_body.get("uiDomain", ""),
            "data_hosting_location": account_body.get("dataHostingLocation", ""),
            "time_zone": account_body.get("timeZone", ""),
            "company_currency": account_body.get("companyCurrency", ""),
        }]

    currency_rows: list[dict] = []
    if "error" not in cur_body:
        for c in cur_body.get("results", []):
            currency_rows.append({
                "code": c.get("code", ""),
                "exchange_rate": c.get("exchangeRate"),
                "active": bool(c.get("active", True)),
            })

    bu_rows: list[dict] = []
    if "error" not in bu_body:
        bus = bu_body.get("results", []) if isinstance(bu_body, dict) else []
        for b in bus:
            bu_rows.append({
                "unit_id": str(b.get("id", "")),
                "name": b.get("name", ""),
                "is_primary": bool(b.get("isPrimary", False)),
            })

    return {
        "schema_version": schema_version,
        "tables": {
            "account_info": account_rows,
            "currencies": currency_rows,
            "business_units": bu_rows,
        },
        "metadata": {"object_type": "platform", "mode": "real",
                     "rows": {"account_info": len(account_rows),
                              "currencies": len(currency_rows),
                              "business_units": len(bu_rows)}},
    }
