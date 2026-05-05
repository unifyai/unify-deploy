"""Employment Hero qualifications and certifications.

The marquee capability for compliance-heavy property-management clients
like ClientZeta.  Each qualification has an expiry date — UK
certifications (Gas Safe, NICEIC, NEBOSH, RICS, ARLA Propertymark) all
have renewal cycles, so the sync table feeds the
``query_local_expiring_qualifications`` analytical query.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


# ---------------------------------------------------------------------------
# Live reads
# ---------------------------------------------------------------------------


@custom_function()
async def list_qualifications(mock: bool = True) -> dict:
    """List the organisation's qualification catalogue (definitions)."""
    if mock:
        return {
            "qualifications": [
                {"id": "qual-mock-1", "name": "Gas Safe Registration",
                 "category": "trade", "validity_months": 12,
                 "is_mandatory_for_role": True},
                {"id": "qual-mock-2", "name": "NICEIC Approved Contractor",
                 "category": "trade", "validity_months": 12,
                 "is_mandatory_for_role": True},
                {"id": "qual-mock-3", "name": "ARLA Propertymark Level 3",
                 "category": "professional", "validity_months": 12,
                 "is_mandatory_for_role": False},
                {"id": "qual-mock-4", "name": "NEBOSH General Certificate",
                 "category": "safety", "validity_months": 36,
                 "is_mandatory_for_role": False},
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path("/qualifications"))
    if "error" in body:
        return body
    return {"qualifications": body.get("data") or body.get("items") or []}


@custom_function()
async def get_qualification(qualification_id: str, mock: bool = True) -> dict:
    """Get one qualification definition by id."""
    if mock:
        return {
            "id": str(qualification_id),
            "name": "Gas Safe Registration",
            "category": "trade",
            "description": "UK Gas Safe Register competence certificate.",
            "validity_months": 12,
            "is_mandatory_for_role": True,
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/qualifications/{qualification_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def list_employee_qualifications(
    employee_id: str | None = None,
    qualification_id: str | None = None,
    expires_before: str | None = None,
    mock: bool = True,
) -> dict:
    """List per-employee qualification records.

    For analytics across many records (especially expiry sweeps), prefer
    ``query_local_expiring_qualifications`` against the synced
    DataManager copy.  Use this for fresh per-employee lookups.
    """
    if mock:
        return {
            "employee_qualifications": [
                {"id": "eq-mock-1", "employee_id": "emp-mock-2",
                 "qualification_id": "qual-mock-1",
                 "issued_at": "2025-06-01", "expires_at": "2026-06-01",
                 "evidence_url": None, "status": "active"},
            ],
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    if employee_id:
        path = org_path(f"/employees/{employee_id}/qualifications")
        params: dict = {}
    else:
        path = org_path("/employee_qualifications")
        params = {}
    if qualification_id:
        params["qualification_id"] = qualification_id
    if expires_before:
        params["expires_before"] = expires_before

    body = await eh_get(path, params=params)
    if "error" in body:
        return body
    return {
        "employee_qualifications": body.get("data") or body.get("items") or [],
    }


# ---------------------------------------------------------------------------
# Sync snapshot
# ---------------------------------------------------------------------------


@custom_function()
async def sync_qualifications(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot the qualification catalogue and per-employee records."""
    import datetime as _dt

    schema_version = "employment-hero.qualifications.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "qualifications": [
                    {"id": "qual-mock-1", "name": "Gas Safe Registration",
                     "category": "trade", "validity_months": 12,
                     "is_mandatory_for_role": True},
                ],
                "employee_qualifications": [
                    {"id": "eq-mock-1", "employee_id": "emp-mock-2",
                     "qualification_id": "qual-mock-1",
                     "qualification_name": "Gas Safe Registration",
                     "issued_at": "2025-06-01",
                     "expires_at": "2026-06-01",
                     "status": "active",
                     "days_to_expiry": 32,
                     "evidence_url": None,
                     "updated_at": started},
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "qualifications",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_paginate,
        eh_get,
        org_path,
        _org_id_or_error,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )

    org_id, err = _org_id_or_error()
    if err is not None:
        err.update({"schema_version": schema_version, "tables": {}})
        return err
    cfg = get_employmenthero_config()

    # Catalogue (definitions)
    quals_raw = await eh_paginate(
        org_path("/qualifications"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    qualifications: list[dict] = []
    qual_name_by_id: dict[str, str] = {}
    for q in quals_raw:
        qualifications.append({
            "id": q.get("id"),
            "name": q.get("name"),
            "category": q.get("category"),
            "description": q.get("description"),
            "validity_months": q.get("validity_months"),
            "is_mandatory_for_role": q.get("is_mandatory_for_role"),
            "created_at": q.get("created_at"),
            "updated_at": q.get("updated_at"),
        })
        if q.get("id"):
            qual_name_by_id[q["id"]] = q.get("name")

    # Per-employee records
    eq_params: dict = {}
    if since:
        eq_params["updated_since"] = since
    eq_raw = await eh_paginate(
        org_path("/employee_qualifications"),
        params=eq_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    today = _dt.date.today()
    employee_qualifications: list[dict] = []
    for r in eq_raw:
        expires_at = r.get("expires_at")
        days_to_expiry = None
        if expires_at:
            try:
                exp = _dt.date.fromisoformat(expires_at[:10])
                days_to_expiry = (exp - today).days
            except (ValueError, TypeError):
                pass
        employee_qualifications.append({
            "id": r.get("id"),
            "employee_id": r.get("employee_id"),
            "qualification_id": r.get("qualification_id"),
            "qualification_name": qual_name_by_id.get(r.get("qualification_id")),
            "issued_at": r.get("issued_at"),
            "expires_at": expires_at,
            "status": r.get("status"),
            "days_to_expiry": days_to_expiry,
            "evidence_url": r.get("evidence_url"),
            "updated_at": r.get("updated_at"),
        })

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "qualifications": qualifications,
            "employee_qualifications": employee_qualifications,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "qualifications",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "qualifications": len(qualifications),
                "employee_qualifications": len(employee_qualifications),
            },
        },
    }
