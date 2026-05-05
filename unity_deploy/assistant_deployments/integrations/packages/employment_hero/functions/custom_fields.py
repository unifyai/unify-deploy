"""Employment Hero custom fields — long-format snapshot.

Each per-employee custom-field value is stored as one row with
``(employee_id, field_id, value)`` so the schema is robust to
org-defined field changes.  Guidance documents the unpivot pattern for
analytics queries.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_employmenthero_custom_field_definitions(mock: bool = True) -> dict:
    if mock:
        return {
            "definitions": [
                {
                    "id": "cf-property",
                    "name": "Primary Property",
                    "data_type": "string",
                    "options": ["Battersea", "Camden", "Manchester"],
                },
                {
                    "id": "cf-asset-class",
                    "name": "Specialist Asset Class",
                    "data_type": "string",
                    "options": ["BTL", "HMO", "Leasehold"],
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
    body = await eh_get(org_path("/custom_fields"))
    if "error" in body:
        return body
    return {"definitions": body.get("data") or body.get("items") or []}


@custom_function()
async def list_employmenthero_custom_field_values(
    employee_id: str | None = None,
    field_id: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "values": [
                {
                    "employee_id": "emp-mock-1",
                    "field_id": "cf-property",
                    "value": "Battersea",
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
    params: dict = {}
    if employee_id:
        params["employee_id"] = employee_id
    if field_id:
        params["field_id"] = field_id
    body = await eh_get(org_path("/custom_field_values"), params=params)
    if "error" in body:
        return body
    return {"values": body.get("data") or body.get("items") or []}


@custom_function()
async def get_employmenthero_employee_custom_field_values(
    employee_id: str,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "employee_id": employee_id,
            "values": [
                {"field_id": "cf-property", "value": "Battersea"},
                {"field_id": "cf-asset-class", "value": "BTL"},
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
    body = await eh_get(org_path(f"/employees/{employee_id}/custom_field_values"))
    if "error" in body:
        return body
    return {
        "employee_id": employee_id,
        "values": body.get("data") or body.get("items") or [],
    }


@custom_function()
async def sync_employmenthero_custom_fields(
    mock: bool = False, since: str | None = None
) -> dict:
    """Snapshot custom-field definitions and per-employee values (long format)."""
    import datetime as _dt

    schema_version = "employment-hero.custom-fields.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "custom_field_definitions": [
                    {
                        "id": "cf-property",
                        "name": "Primary Property",
                        "data_type": "string",
                        "options_json": "['Battersea','Camden','Manchester']",
                        "updated_at": started,
                    },
                ],
                "custom_field_values": [
                    {
                        "employee_id": "emp-mock-1",
                        "field_id": "cf-property",
                        "value": "Battersea",
                        "updated_at": started,
                    },
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "custom_fields",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_paginate,
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

    defs_raw = await eh_paginate(
        org_path("/custom_fields"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    custom_field_definitions = [
        {
            "id": d.get("id"),
            "name": d.get("name"),
            "data_type": d.get("data_type"),
            "options_json": str(d.get("options")) if d.get("options") else None,
            "is_required": d.get("is_required"),
            "applies_to": d.get("applies_to"),
            "updated_at": d.get("updated_at"),
        }
        for d in defs_raw
    ]

    values_params: dict = {}
    if since:
        values_params["updated_since"] = since
    values_raw = await eh_paginate(
        org_path("/custom_field_values"),
        params=values_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    custom_field_values: list[dict] = []
    for v in values_raw:
        # Long format: one row per (employee, field).  Coerce value to
        # string for portability; the typed form lives behind the
        # definition + data_type pair.
        raw_val = v.get("value")
        if raw_val is None:
            value_str = None
        elif isinstance(raw_val, (str, int, float, bool)):
            value_str = str(raw_val)
        else:
            value_str = str(raw_val)
        custom_field_values.append(
            {
                "employee_id": v.get("employee_id"),
                "field_id": v.get("field_id"),
                "value": value_str,
                "updated_at": v.get("updated_at"),
            }
        )

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "custom_field_definitions": custom_field_definitions,
            "custom_field_values": custom_field_values,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "custom_fields",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "custom_field_definitions": len(custom_field_definitions),
                "custom_field_values": len(custom_field_values),
            },
        },
    }
