"""Employment Hero employee personal details — emergency contacts, dependants,
visa, probation.  HIGH-TIER: snapshot strips PII; live reads return full data
but the assistant must follow the redaction rules in
``guidance/sensitive_data.md``.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_emergency_contacts(employee_id: str, mock: bool = True) -> dict:
    if mock:
        return {
            "contacts": [
                {
                    "id": "ec-1",
                    "employee_id": employee_id,
                    "name": "Jane Example",
                    "relationship": "spouse",
                    "phone": "+44 7700 900200",
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
    body = await eh_get(org_path(f"/employees/{employee_id}/emergency_contacts"))
    if "error" in body:
        return body
    return {"contacts": body.get("data") or body.get("items") or []}


@custom_function()
async def list_dependants(employee_id: str, mock: bool = True) -> dict:
    if mock:
        return {
            "dependants": [
                {
                    "id": "dep-1",
                    "employee_id": employee_id,
                    "relationship": "child",
                    "date_of_birth": "2018-05-12",
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
    body = await eh_get(org_path(f"/employees/{employee_id}/dependants"))
    if "error" in body:
        return body
    return {"dependants": body.get("data") or body.get("items") or []}


@custom_function()
async def get_visa_details(employee_id: str, mock: bool = True) -> dict:
    if mock:
        return {
            "employee_id": employee_id,
            "has_visa": False,
            "right_to_work_status": "british_citizen",
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/employees/{employee_id}/visa_details"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def get_probation_status(employee_id: str, mock: bool = True) -> dict:
    if mock:
        return {
            "employee_id": employee_id,
            "is_on_probation": False,
            "probation_end_date": "2024-03-01",
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/employees/{employee_id}/probation"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def sync_employee_personal(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot personal-detail tables with PII redaction.

    Emergency contacts: name + phone removed; replaced with length+hash
      so dedup/correlate works in DataManager analytics without exposure.
    Dependants: stored as counts per employee.
    Visa: status + key dates only.
    Probation: status + key dates only.
    """
    import datetime as _dt
    import hashlib

    schema_version = "employment-hero.employee-personal.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    def _redact(text: str | None) -> dict:
        if not text:
            return {"length": 0, "hash": None}
        return {
            "length": len(text),
            "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        }

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "emergency_contacts": [
                    {
                        "id": "ec-1",
                        "employee_id": "emp-mock-1",
                        "relationship": "spouse",
                        "name_redacted_length": 12,
                        "name_redacted_hash": "abcd1234",
                        "phone_redacted_length": 13,
                        "phone_redacted_hash": "efef5678",
                        "updated_at": started,
                    },
                ],
                "dependants_counts": [
                    {"employee_id": "emp-mock-1", "count": 1, "updated_at": started},
                ],
                "visa_status": [
                    {
                        "employee_id": "emp-mock-1",
                        "has_visa": False,
                        "right_to_work_status": "british_citizen",
                        "visa_expires_at": None,
                        "updated_at": started,
                    },
                ],
                "probation_status": [
                    {
                        "employee_id": "emp-mock-1",
                        "is_on_probation": False,
                        "probation_end_date": "2024-03-01",
                        "updated_at": started,
                    },
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "employee_personal",
                "started_at": started,
                "mode": "mock",
                "redacted_fields": ["name", "phone"],
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
    redact_personal = cfg["redact_employee_personal"]

    # Pull employee-id list to iterate per-employee endpoints
    emps_raw = await eh_paginate(
        org_path("/employees"),
        params={"include_terminated": False},
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    emp_ids = [e.get("id") for e in emps_raw if e.get("id")]

    emergency_contacts: list[dict] = []
    dependants_counts: list[dict] = []
    visa_status: list[dict] = []
    probation_status: list[dict] = []

    for eid in emp_ids:
        # Emergency contacts
        ec_body = await eh_get(org_path(f"/employees/{eid}/emergency_contacts"))
        if "error" not in ec_body:
            for ec in ec_body.get("data") or ec_body.get("items") or []:
                row = {
                    "id": ec.get("id"),
                    "employee_id": eid,
                    "relationship": ec.get("relationship"),
                    "updated_at": ec.get("updated_at"),
                }
                if redact_personal:
                    name_red = _redact(ec.get("name"))
                    phone_red = _redact(ec.get("phone"))
                    row.update(
                        {
                            "name_redacted_length": name_red["length"],
                            "name_redacted_hash": name_red["hash"],
                            "phone_redacted_length": phone_red["length"],
                            "phone_redacted_hash": phone_red["hash"],
                        }
                    )
                else:
                    row["name"] = ec.get("name")
                    row["phone"] = ec.get("phone")
                emergency_contacts.append(row)

        # Dependants - counts only
        dep_body = await eh_get(org_path(f"/employees/{eid}/dependants"))
        if "error" not in dep_body:
            count = len(dep_body.get("data") or dep_body.get("items") or [])
            dependants_counts.append(
                {
                    "employee_id": eid,
                    "count": count,
                    "updated_at": started,
                }
            )

        # Visa
        visa_body = await eh_get(org_path(f"/employees/{eid}/visa_details"))
        if "error" not in visa_body:
            v = visa_body.get("data") or visa_body
            visa_status.append(
                {
                    "employee_id": eid,
                    "has_visa": v.get("has_visa") or bool(v.get("visa_type")),
                    "right_to_work_status": v.get("right_to_work_status"),
                    "visa_type": v.get("visa_type"),
                    "visa_expires_at": v.get("expires_at") or v.get("visa_expires_at"),
                    "updated_at": v.get("updated_at"),
                }
            )

        # Probation
        prob_body = await eh_get(org_path(f"/employees/{eid}/probation"))
        if "error" not in prob_body:
            p = prob_body.get("data") or prob_body
            probation_status.append(
                {
                    "employee_id": eid,
                    "is_on_probation": p.get("is_on_probation"),
                    "probation_end_date": p.get("probation_end_date"),
                    "review_outcome": p.get("review_outcome"),
                    "updated_at": p.get("updated_at"),
                }
            )

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "emergency_contacts": emergency_contacts,
            "dependants_counts": dependants_counts,
            "visa_status": visa_status,
            "probation_status": probation_status,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "employee_personal",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "redacted_fields": (
                ["emergency_contacts.name", "emergency_contacts.phone"]
                if redact_personal
                else []
            ),
            "row_counts": {
                "emergency_contacts": len(emergency_contacts),
                "dependants_counts": len(dependants_counts),
                "visa_status": len(visa_status),
                "probation_status": len(probation_status),
            },
        },
    }
