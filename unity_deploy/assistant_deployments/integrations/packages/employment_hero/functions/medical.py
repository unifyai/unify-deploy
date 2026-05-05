"""Employment Hero medical disclosures — CRITICAL TIER, live-only.

Counts and metadata only by default.  Free-text disclosure content is
never returned by ``list_medical_disclosures_count`` and only returned
by ``get_medical_disclosure`` after explicit user authorisation.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_medical_disclosures_count(
    employee_id: str | None = None,
    mock: bool = True,
) -> dict:
    """Return counts of disclosures per employee — never the content."""
    if mock:
        return {
            "counts": [
                {"employee_id": "emp-mock-1", "count": 0},
                {
                    "employee_id": "emp-mock-2",
                    "count": 1,
                    "_warning": "Disclosure exists; content not returned.",
                },
            ]
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
        body = await eh_get(
            org_path(f"/employees/{employee_id}/medical_disclosures"),
        )
        if "error" in body:
            return body
        rows = body.get("data") or body.get("items") or []
        return {
            "counts": [
                {"employee_id": employee_id, "count": len(rows)},
            ]
        }

    # Org-wide count: iterate employees - tier-gated 403 returns empty.
    emps = await eh_get(org_path("/employees?include_terminated=false&limit=200"))
    if "error" in emps:
        return emps
    counts: list[dict] = []
    for e in emps.get("data") or emps.get("items") or []:
        eid = e.get("id")
        if not eid:
            continue
        body = await eh_get(
            org_path(f"/employees/{eid}/medical_disclosures"),
        )
        if "error" in body:
            continue
        rows = body.get("data") or body.get("items") or []
        counts.append({"employee_id": eid, "count": len(rows)})
    return {"counts": counts}


@custom_function()
async def get_medical_disclosure(
    disclosure_id: str,
    confirm_user_authorised: bool = False,
    mock: bool = True,
) -> dict:
    """Get one medical disclosure.  CRITICAL TIER.

    Returns the content only after explicit user authorisation.
    """
    if mock:
        return {
            "id": str(disclosure_id),
            "employee_id": "emp-mock-2",
            "category": "physical",
            "recorded_at": "2025-09-01",
            "summary": (
                "Mock disclosure summary — only returned with "
                "confirm_user_authorised=True."
            ),
        }

    if not confirm_user_authorised:
        return {
            "error": "Refused: confirm_user_authorised=True is required.",
            "hint": (
                "Medical disclosures are critical-tier.  Confirm with the "
                "user that they have authority (HR / Health & Safety lead) "
                "and that the employee has consented to disclosure access "
                "before retrying."
            ),
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/medical_disclosures/{disclosure_id}"))
    if "error" in body:
        return body
    return body.get("data") or body
