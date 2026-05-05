"""Employment Hero HR policies and acknowledgements."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_policies(mock: bool = True) -> dict:
    if mock:
        return {"policies": [
            {"id": "pol-1", "name": "Code of Conduct", "version": "3",
             "effective_from": "2025-01-01", "country": "GB"},
            {"id": "pol-2", "name": "GDPR Data Handling", "version": "2",
             "effective_from": "2024-06-01", "country": "GB"},
            {"id": "pol-3", "name": "Lone Working", "version": "1",
             "effective_from": "2025-08-15", "country": "GB"},
        ]}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path("/policies"))
    if "error" in body:
        return body
    return {"policies": body.get("data") or body.get("items") or []}


@custom_function()
async def get_policy(policy_id: str, mock: bool = True) -> dict:
    if mock:
        return {"id": str(policy_id), "name": "Code of Conduct",
                "version": "3", "effective_from": "2025-01-01",
                "url": "https://example.test/policies/code-of-conduct"}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/policies/{policy_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def list_policy_acknowledgements(
    policy_id: str | None = None,
    employee_id: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {"acknowledgements": [
            {"policy_id": "pol-1", "employee_id": "emp-mock-1",
             "acknowledged_at": "2025-04-12T10:00:00Z"},
        ]}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    params: dict = {}
    if policy_id:
        params["policy_id"] = policy_id
    if employee_id:
        params["employee_id"] = employee_id
    body = await eh_get(org_path("/policy_acknowledgements"), params=params)
    if "error" in body:
        return body
    return {"acknowledgements": body.get("data") or body.get("items") or []}


@custom_function()
async def acknowledge_policy(
    policy_id: str,
    employee_id: str,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Record a policy acknowledgement.  HIGH-STAKES WRITE."""
    if mock:
        return {"policy_id": policy_id, "employee_id": employee_id,
                "acknowledged_at": "2026-04-30T10:00:00Z", "_mocked": True}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_post, org_path, _org_id_or_error,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )
    cfg = get_employmenthero_config()
    if not cfg["allow_high_stakes_writes"]:
        return {"error": "Refused: EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES is not true."}
    if not confirm:
        return {"error": "Refused: confirm=True is required for live mutations."}
    _, err = _org_id_or_error()
    if err is not None:
        return err
    return await eh_post(
        org_path(f"/policies/{policy_id}/acknowledgements"),
        {"employee_id": employee_id},
    )


@custom_function()
async def sync_policies(mock: bool = False, since: str | None = None) -> dict:
    import datetime as _dt
    schema_version = "employment-hero.policies.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "policies": [{"id": "pol-1", "name": "Code of Conduct",
                              "version": "3", "effective_from": "2025-01-01",
                              "updated_at": started}],
                "policy_acknowledgements": [{"policy_id": "pol-1",
                                              "employee_id": "emp-mock-1",
                                              "acknowledged_at": "2025-04-12T10:00:00Z"}],
            },
            "metadata": {"integration": "employment_hero",
                         "object_type": "policies", "started_at": started,
                         "mode": "mock"},
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

    pol_raw = await eh_paginate(
        org_path("/policies"),
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    policies = [{
        "id": p.get("id"), "name": p.get("name"),
        "version": p.get("version"),
        "effective_from": p.get("effective_from"),
        "country": p.get("country"),
        "url": p.get("url"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
    } for p in pol_raw]

    ack_params: dict = {}
    if since:
        ack_params["acknowledged_since"] = since
    ack_raw = await eh_paginate(
        org_path("/policy_acknowledgements"),
        params=ack_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    acks = [{
        "policy_id": a.get("policy_id"),
        "employee_id": a.get("employee_id"),
        "acknowledged_at": a.get("acknowledged_at"),
        "policy_version": a.get("policy_version"),
    } for a in ack_raw]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"policies": policies, "policy_acknowledgements": acks},
        "metadata": {"integration": "employment_hero", "object_type": "policies",
                     "organisation_id": org_id, "started_at": started,
                     "finished_at": finished, "since": since,
                     "row_counts": {"policies": len(policies),
                                    "policy_acknowledgements": len(acks)}},
    }
