"""Employment Hero policies sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_policies(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    import datetime as _dt

    schema_version = "employment-hero.policies.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        return {
            "schema_version": schema_version,
            "tables": {
                "policies": [
                    {
                        "id": "pol-1",
                        "name": "Code of Conduct",
                        "version": "3",
                        "effective_from": "2025-01-01",
                        "updated_at": started,
                    },
                ],
                "policy_acknowledgements": [
                    {
                        "policy_id": "pol-1",
                        "employee_id": "emp-mock-1",
                        "acknowledged_at": "2025-04-12T10:00:00Z",
                    },
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "policies",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unify_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_paginate,
        org_path,
        _org_id_or_error,
    )
    from unify_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
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
    policies = [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "version": p.get("version"),
            "effective_from": p.get("effective_from"),
            "country": p.get("country"),
            "url": p.get("url"),
            "created_at": p.get("created_at"),
            "updated_at": p.get("updated_at"),
        }
        for p in pol_raw
    ]

    ack_params: dict = {}
    if since:
        ack_params["acknowledged_since"] = since
    ack_raw = await eh_paginate(
        org_path("/policy_acknowledgements"),
        params=ack_params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    acks = [
        {
            "policy_id": a.get("policy_id"),
            "employee_id": a.get("employee_id"),
            "acknowledged_at": a.get("acknowledged_at"),
            "policy_version": a.get("policy_version"),
        }
        for a in ack_raw
    ]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"policies": policies, "policy_acknowledgements": acks},
        "metadata": {
            "integration": "employment_hero",
            "object_type": "policies",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {
                "policies": len(policies),
                "policy_acknowledgements": len(acks),
            },
        },
    }
