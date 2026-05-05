"""Employment Hero individual payslips — CRITICAL TIER, live-only.

Never synced into DataManager.  Returned values are masked at the
function boundary even if the API would return them in full; the
assistant relays only what the user is allowed to see and follows the
refusal patterns in ``guidance/sensitive_data.md``.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_employmenthero_employee_payslips(
    employee_id: str,
    confirm_user_authorised: bool = False,
    limit: int = 12,
    mock: bool = True,
) -> dict:
    """List payslips for one employee.  CRITICAL TIER.

    Returns masked amounts; exact values must be viewed in Employment
    Hero directly.
    """
    if mock:
        return {
            "employee_id": employee_id,
            "payslips": [
                {
                    "id": "ps-1",
                    "period_start": "2026-04-01",
                    "period_end": "2026-04-30",
                    "gross_masked": "£***.**",
                    "net_masked": "£***.**",
                    "tax_masked": "£***.**",
                    "currency": "GBP",
                    "issued_at": "2026-04-29T16:00:00Z",
                    "_warning": "Masked payslip — view exact figures in Employment Hero.",
                },
            ],
        }

    if not confirm_user_authorised:
        return {
            "error": "Refused: confirm_user_authorised=True is required.",
            "hint": (
                "Payslips are critical-tier.  Confirm with the user that "
                "they have authority (payroll admin or the payslip owner) "
                "before retrying."
            ),
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._payslip_helpers import (
        mask_currency,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(
        org_path(f"/employees/{employee_id}/payslips"),
        params={"limit": min(limit, 100)},
    )
    if "error" in body:
        return body
    rows = body.get("data") or body.get("items") or []
    masked = []
    for p in rows:
        currency = p.get("currency") or "GBP"
        masked.append(
            {
                "id": p.get("id"),
                "period_start": p.get("period_start"),
                "period_end": p.get("period_end"),
                "gross_masked": mask_currency(p.get("gross"), currency),
                "net_masked": mask_currency(p.get("net"), currency),
                "tax_masked": mask_currency(p.get("tax"), currency),
                "currency": currency,
                "issued_at": p.get("issued_at"),
                "_warning": (
                    "Masked payslip.  Exact line-item values must be viewed "
                    "in Employment Hero directly."
                ),
            }
        )
    return {"employee_id": employee_id, "payslips": masked}


@custom_function()
async def get_employmenthero_payslip(
    payslip_id: str,
    confirm_user_authorised: bool = False,
    mock: bool = True,
) -> dict:
    """Get one payslip.  CRITICAL TIER, masked return."""
    if mock:
        return {
            "id": str(payslip_id),
            "period_start": "2026-04-01",
            "period_end": "2026-04-30",
            "gross_masked": "£***.**",
            "net_masked": "£***.**",
            "tax_masked": "£***.**",
            "currency": "GBP",
            "_warning": "Masked payslip.",
        }

    if not confirm_user_authorised:
        return {
            "error": "Refused: confirm_user_authorised=True is required.",
            "hint": "Payslips are critical-tier.",
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._payslip_helpers import (
        mask_currency,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/payslips/{payslip_id}"))
    if "error" in body:
        return body
    p = body.get("data") or body
    currency = p.get("currency") or "GBP"
    return {
        "id": p.get("id"),
        "employee_id": p.get("employee_id"),
        "period_start": p.get("period_start"),
        "period_end": p.get("period_end"),
        "gross_masked": mask_currency(p.get("gross"), currency),
        "net_masked": mask_currency(p.get("net"), currency),
        "tax_masked": mask_currency(p.get("tax"), currency),
        "currency": currency,
        "issued_at": p.get("issued_at"),
        "_warning": "Masked payslip — exact line-items must be viewed in EH.",
    }
