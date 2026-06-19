"""Tier / capability probe for Employment Hero.

Sweeps representative endpoints for each sync object type and records
which return 200 vs. 403 / not-supported.  Cached in DataManager at
``EmploymentHero/Workforce/Meta/Capabilities`` for
``EMPLOYMENTHERO_TIER_PROBE_TTL_SECONDS`` (default 24h).

Underscore-prefixed so FunctionManager skips this file at discovery.
The runtime-callable wrapper lives in ``sync.py`` as ``probe_tier``.
"""

from __future__ import annotations

# Probe endpoint per sync-object key.  Each is a cheap GET that should
# succeed if the token has the relevant scope and EH module is enabled
# on the customer's plan.
_PROBE_ENDPOINTS: dict[str, str] = {
    "workforce": "/employees?limit=1",
    "employee_personal": "/emergency_contacts?limit=1",
    "employee_notes": "/notes?limit=1",
    "leave": "/leave_categories?limit=1",
    "timesheets": "/timesheet_entries?limit=1",
    "expenses": "/expense_categories?limit=1",
    "policies": "/policies?limit=1",
    "documents": "/documents?limit=1",
    "custom_fields": "/custom_fields?limit=1",
    "onboarding": "/onboardings?limit=1",
    "qualifications": "/qualifications?limit=1",
    "performance": "/reviews?limit=1",
    "recognition": "/cheers?limit=1",
    "surveys": "/surveys?limit=1",
    "learning": "/courses?limit=1",
    "recruitment": "/jobs?limit=1",
    "pay": "/pay_runs?limit=1",
}


async def run_tier_probe() -> dict:
    """Sweep every probe endpoint; return ``{object_key: status_code}``.

    Caller is responsible for caching the result in DataManager.
    """
    from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    org_id, err = _org_id_or_error()
    if org_id is None:
        return {"_error": err}

    results: dict[str, dict] = {}
    for key, suffix in _PROBE_ENDPOINTS.items():
        body = await eh_get(org_path(suffix))
        if "error" in body:
            status = body.get("status_code")
            results[key] = {
                "available": False,
                "status_code": status,
                "hint": body.get("hint"),
            }
        else:
            results[key] = {"available": True, "status_code": 200}
    return results
