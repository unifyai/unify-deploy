"""Employment Hero pay runs and employment terms.

CRITICAL TIER.  Pay runs are summarised (totals only).  Employment
terms snapshot bands exact rates into ranges so DataManager analytics
work without exposing exact salaries.

Individual payslips live in ``payslips.py`` and are live-only.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


# ---------------------------------------------------------------------------
# Pay rate banding
# ---------------------------------------------------------------------------


@custom_function()
def _band_rate(amount: float | None, currency: str = "GBP") -> str:
    """Convert exact rate to coarse band (e.g. '<45,000 GBP').

    Bands are inlined per FunctionManager isolation rule — no module-level
    globals referenced from within a function body.  Decorated so the
    sync_pay function can depend on it as a registered helper.
    """
    bands = [
        25_000, 35_000, 45_000, 60_000, 80_000,
        100_000, 130_000, 170_000, 220_000,
    ]
    if amount is None:
        return "unknown"
    for upper in bands:
        if amount < upper:
            return f"<{upper:,} {currency}"
    return f">={bands[-1]:,} {currency}"


# ---------------------------------------------------------------------------
# Pay runs
# ---------------------------------------------------------------------------


@custom_function()
async def list_pay_runs(
    period: str | None = None,
    status: str | None = None,
    limit: int = 24,
    mock: bool = True,
) -> dict:
    """List pay runs (summary only).

    For exact figures, use the live API directly.  Returned amounts are
    period-totals across the organisation, never per-employee.
    """
    if mock:
        return {"pay_runs": [
            {"id": "pr-1", "period_start": "2026-04-01",
             "period_end": "2026-04-30", "status": "finalised",
             "employee_count": 124,
             "total_gross_pay": 412_500.00, "total_tax": 91_300.00,
             "total_net_pay": 321_200.00, "currency": "GBP",
             "finalised_at": "2026-04-29T16:00:00Z"},
        ]}

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    params: dict = {"limit": min(limit, 100)}
    if period:
        params["period"] = period
    if status:
        params["status"] = status
    body = await eh_get(org_path("/pay_runs"), params=params)
    if "error" in body:
        return body
    return {"pay_runs": body.get("data") or body.get("items") or []}


@custom_function()
async def get_pay_run(pay_run_id: str, mock: bool = True) -> dict:
    """Get one pay run (org-totals only)."""
    if mock:
        return {"id": str(pay_run_id),
                "period_start": "2026-04-01", "period_end": "2026-04-30",
                "status": "finalised", "employee_count": 124,
                "total_gross_pay": 412_500.00, "currency": "GBP",
                "finalised_at": "2026-04-29T16:00:00Z"}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/pay_runs/{pay_run_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def list_pay_categories(mock: bool = True) -> dict:
    if mock:
        return {"categories": [
            {"id": "pc-1", "name": "Ordinary Hours", "is_taxable": True,
             "is_pensionable": True},
            {"id": "pc-2", "name": "Overtime", "is_taxable": True,
             "is_pensionable": False},
            {"id": "pc-3", "name": "Holiday Pay", "is_taxable": True,
             "is_pensionable": True},
        ]}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path("/pay_categories"))
    if "error" in body:
        return body
    return {"categories": body.get("data") or body.get("items") or []}


# ---------------------------------------------------------------------------
# Employment terms — exact rate live; sync bands.
# ---------------------------------------------------------------------------


@custom_function()
async def get_employment_terms(
    employee_id: str,
    confirm_user_authorised: bool = False,
    mock: bool = True,
) -> dict:
    """Read an employee's exact employment terms (rate, classification).

    CRITICAL TIER.  Pass ``confirm_user_authorised=True`` only after
    confirming with the user that they have authority (HR admin or the
    employee themselves).
    """
    if mock:
        return {"employee_id": employee_id,
                "classification": "full_time",
                "rate_type": "salary",
                "annualised_amount": 42_500.00,
                "currency": "GBP",
                "effective_from": "2025-04-01",
                "effective_to": None,
                "_warning": "Mock data; live API returns exact figures."}

    if not confirm_user_authorised:
        return {
            "error": "Refused: confirm_user_authorised=True is required.",
            "hint": (
                "This function returns exact pay figures.  Confirm with "
                "the user that they're authorised to view this employee's "
                "terms (HR admin or the employee themselves) before retrying."
            ),
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/employees/{employee_id}/employment_terms"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def list_employment_terms(
    confirm_user_authorised: bool = False,
    mock: bool = True,
) -> dict:
    """List employment terms across all employees (CRITICAL TIER)."""
    if mock:
        return {"terms": [
            {"employee_id": "emp-mock-1",
             "classification": "full_time", "rate_type": "salary",
             "annualised_amount": 42_500.00, "currency": "GBP",
             "effective_from": "2025-04-01"},
        ]}

    if not confirm_user_authorised:
        return {
            "error": "Refused: confirm_user_authorised=True is required.",
            "hint": "Bulk pay-rate data — confirm authorisation before retrying.",
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path("/employment_terms"))
    if "error" in body:
        return body
    return {"terms": body.get("data") or body.get("items") or []}


# ---------------------------------------------------------------------------
# Sync — pay run aggregates + banded employment terms
# ---------------------------------------------------------------------------


@custom_function()
async def sync_pay(mock: bool = False, since: str | None = None) -> dict:
    """Snapshot pay run aggregates + banded employment terms.

    Pay runs: org-level totals (gross/tax/net) per period.  No
    per-employee figures.
    Employment terms: rate banded into a coarse range so DataManager
    analytics work without exposing exact salaries.
    """
    import datetime as _dt
    schema_version = "employment-hero.pay.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "pay_runs": [{"id": "pr-1",
                               "period_start": "2026-04-01",
                               "period_end": "2026-04-30",
                               "status": "finalised",
                               "employee_count": 124,
                               "total_gross_pay": 412_500.00,
                               "total_tax": 91_300.00,
                               "total_net_pay": 321_200.00,
                               "currency": "GBP",
                               "finalised_at": "2026-04-29T16:00:00Z",
                               "updated_at": started}],
                "pay_categories": [{"id": "pc-1", "name": "Ordinary Hours",
                                     "is_taxable": True, "is_pensionable": True,
                                     "updated_at": started}],
                "employment_terms": [{"employee_id": "emp-mock-1",
                                       "effective_from": "2025-04-01",
                                       "effective_to": None,
                                       "classification": "full_time",
                                       "rate_type": "salary",
                                       "annualised_band": "<45,000 GBP",
                                       "currency": "GBP",
                                       "updated_at": started}],
            },
            "metadata": {"integration": "employment_hero",
                         "object_type": "pay",
                         "started_at": started, "mode": "mock",
                         "banded_fields": [
                             "employment_terms.annualised_amount -> annualised_band",
                         ]},
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_paginate, org_path, _org_id_or_error,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )
    org_id, err = _org_id_or_error()
    if err is not None:
        err.update({"schema_version": schema_version, "tables": {}})
        return err
    cfg = get_employmenthero_config()
    page_size = cfg["api_page_size"]
    max_pages = cfg["max_pages_per_sync"]
    apply_bands = cfg["pay_rate_bands"]

    pr_params: dict = {}
    if since:
        pr_params["updated_since"] = since
    pr_raw = await eh_paginate(
        org_path("/pay_runs"), params=pr_params,
        page_size=page_size, max_pages=max_pages,
    )
    pay_runs = [{
        "id": p.get("id"),
        "period_start": p.get("period_start"),
        "period_end": p.get("period_end"),
        "status": p.get("status"),
        "employee_count": p.get("employee_count"),
        "total_gross_pay": p.get("total_gross_pay"),
        "total_tax": p.get("total_tax"),
        "total_ni": p.get("total_ni") or p.get("total_employee_ni"),
        "total_pension": p.get("total_pension"),
        "total_net_pay": p.get("total_net_pay"),
        "currency": p.get("currency"),
        "finalised_at": p.get("finalised_at"),
        "updated_at": p.get("updated_at"),
    } for p in pr_raw]

    pc_raw = await eh_paginate(
        org_path("/pay_categories"),
        page_size=page_size, max_pages=max_pages,
    )
    pay_categories = [{
        "id": pc.get("id"), "name": pc.get("name"),
        "is_taxable": pc.get("is_taxable"),
        "is_pensionable": pc.get("is_pensionable"),
        "updated_at": pc.get("updated_at"),
    } for pc in pc_raw]

    et_params: dict = {}
    if since:
        et_params["updated_since"] = since
    et_raw = await eh_paginate(
        org_path("/employment_terms"), params=et_params,
        page_size=page_size, max_pages=max_pages,
    )
    employment_terms: list[dict] = []
    for t in et_raw:
        amount = t.get("annualised_amount") or t.get("amount")
        currency = t.get("currency") or "GBP"
        row = {
            "employee_id": t.get("employee_id"),
            "effective_from": t.get("effective_from"),
            "effective_to": t.get("effective_to"),
            "classification": t.get("classification"),
            "rate_type": t.get("rate_type"),
            "currency": currency,
            "updated_at": t.get("updated_at"),
        }
        if apply_bands:
            row["annualised_band"] = _band_rate(amount, currency)
        else:
            row["annualised_amount"] = amount
        employment_terms.append(row)

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "pay_runs": pay_runs,
            "pay_categories": pay_categories,
            "employment_terms": employment_terms,
        },
        "metadata": {
            "integration": "employment_hero", "object_type": "pay",
            "organisation_id": org_id,
            "started_at": started, "finished_at": finished, "since": since,
            "banded_fields": (
                ["employment_terms.annualised_amount -> annualised_band"]
                if apply_bands else []
            ),
            "row_counts": {
                "pay_runs": len(pay_runs),
                "pay_categories": len(pay_categories),
                "employment_terms": len(employment_terms),
            },
        },
    }
