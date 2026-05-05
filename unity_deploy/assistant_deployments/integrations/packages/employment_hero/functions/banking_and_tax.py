"""Employment Hero banking, super, and tax declarations — CRITICAL TIER.

Live-only.  Returned values are tail-masked (e.g. ``****1234``).
Tax fields including UK National Insurance numbers and AU Tax File
Numbers are never echoed in full — only "PRESENT"/"MISSING".
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def get_banking_details(
    employee_id: str,
    confirm_user_authorised: bool = False,
    mock: bool = True,
) -> dict:
    """Read an employee's banking details.  CRITICAL TIER, masked return."""
    if mock:
        return {
            "employee_id": employee_id,
            "account_holder_name": "PRESENT",
            "sort_code_masked": "**-**-12",
            "account_number_masked": "****1234",
            "bank_name": "Example Bank UK",
            "_warning": (
                "Masked banking details.  Exact values must be viewed in "
                "Employment Hero directly."
            ),
        }

    if not confirm_user_authorised:
        return {
            "error": "Refused: confirm_user_authorised=True is required.",
            "hint": (
                "Banking details are critical-tier.  Confirm with the user "
                "that they have authority (payroll admin or the employee "
                "themselves) before retrying."
            ),
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    def _mask_tail(value, keep=4):
        if not value:
            return None
        s = str(value)
        if len(s) <= keep:
            return "*" * len(s)
        return ("*" * (len(s) - keep)) + s[-keep:]

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/employees/{employee_id}/banking_details"))
    if "error" in body:
        return body
    b = body.get("data") or body
    return {
        "employee_id": employee_id,
        "account_holder_name": "PRESENT" if b.get("account_holder_name") else "MISSING",
        "sort_code_masked": _mask_tail(b.get("sort_code") or b.get("bsb"), keep=2),
        "account_number_masked": _mask_tail(b.get("account_number"), keep=4),
        "bank_name": b.get("bank_name"),
        "_warning": (
            "Masked banking details.  Exact values must be viewed in "
            "Employment Hero directly."
        ),
    }


@custom_function()
async def list_super_funds(
    employee_id: str | None = None,
    confirm_user_authorised: bool = False,
    mock: bool = True,
) -> dict:
    """List superannuation / pension fund records (AU + UK).  CRITICAL TIER."""
    if mock:
        return {
            "super_funds": [
                {
                    "employee_id": "emp-mock-1",
                    "scheme_name": "NEST Pensions (UK)",
                    "member_id_masked": "****5678",
                    "contribution_pct": 5.0,
                    "_warning": "Masked pension record.",
                },
            ]
        }

    if not confirm_user_authorised:
        return {"error": "Refused: confirm_user_authorised=True is required."}

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    def _mask_tail(value, keep=4):
        if not value:
            return None
        s = str(value)
        if len(s) <= keep:
            return "*" * len(s)
        return ("*" * (len(s) - keep)) + s[-keep:]

    _, err = _org_id_or_error()
    if err is not None:
        return err
    if employee_id:
        path = org_path(f"/employees/{employee_id}/super_funds")
    else:
        path = org_path("/super_funds")
    body = await eh_get(path)
    if "error" in body:
        return body
    rows = body.get("data") or body.get("items") or []
    masked = []
    for r in rows:
        masked.append(
            {
                "employee_id": r.get("employee_id"),
                "scheme_name": r.get("scheme_name") or r.get("fund_name"),
                "member_id_masked": _mask_tail(
                    r.get("member_id") or r.get("usi"),
                    keep=4,
                ),
                "contribution_pct": r.get("contribution_pct"),
                "is_active": r.get("is_active"),
            }
        )
    return {"super_funds": masked}


@custom_function()
async def get_tax_declaration(
    employee_id: str,
    confirm_user_authorised: bool = False,
    mock: bool = True,
) -> dict:
    """Read an employee's tax declaration.  CRITICAL TIER.

    UK NI numbers and AU TFNs are never echoed — only PRESENT/MISSING +
    declaration metadata.
    """
    if mock:
        return {
            "employee_id": employee_id,
            "country": "GB",
            "tax_code": "1257L",
            "ni_number_status": "PRESENT",
            "tfn_status": "MISSING",
            "is_student_loan": False,
            "declaration_signed_at": "2023-09-01",
            "_warning": (
                "Masked tax declaration.  NI numbers / TFNs / personal "
                "tax codes must be viewed in Employment Hero directly."
            ),
        }

    if not confirm_user_authorised:
        return {
            "error": "Refused: confirm_user_authorised=True is required.",
            "hint": "Tax declarations are critical-tier.",
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/employees/{employee_id}/tax_declaration"))
    if "error" in body:
        return body
    t = body.get("data") or body
    return {
        "employee_id": employee_id,
        "country": t.get("country"),
        "tax_code": t.get("tax_code"),
        "ni_number_status": "PRESENT" if t.get("ni_number") else "MISSING",
        "tfn_status": "PRESENT" if t.get("tfn") else "MISSING",
        "is_student_loan": t.get("is_student_loan"),
        "declaration_signed_at": t.get("declaration_signed_at") or t.get("signed_at"),
        "_warning": (
            "Masked tax declaration.  NI numbers / TFNs / personal tax "
            "codes must be viewed in Employment Hero directly."
        ),
    }
