"""Employment Hero recruitment — jobs, applicants, offers, interview stages.

Applicants are external PII.  Snapshot redacts identifying fields by
default (controlled by ``EMPLOYMENTHERO_RECRUITMENT_REDACT_PII``) and
windows by ``EMPLOYMENTHERO_RECRUITMENT_RETENTION_DAYS`` so we don't
hold applicant data indefinitely in DataManager.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_employmenthero_jobs(
    status: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "jobs": [
                {
                    "id": "job-1",
                    "title": "Maintenance Operative — Camden",
                    "status": "open",
                    "posted_at": "2026-04-01",
                    "closes_at": "2026-05-15",
                    "location_id": "loc-mock-2",
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
    if status:
        params["status"] = status
    body = await eh_get(org_path("/jobs"), params=params)
    if "error" in body:
        return body
    return {"jobs": body.get("data") or body.get("items") or []}


@custom_function()
async def get_employmenthero_job(job_id: str, mock: bool = True) -> dict:
    if mock:
        return {
            "id": str(job_id),
            "title": "Maintenance Operative — Camden",
            "status": "open",
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/jobs/{job_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def list_employmenthero_applicants(
    job_id: str | None = None,
    stage: str | None = None,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "applicants": [
                {
                    "id": "app-1",
                    "job_id": "job-1",
                    "first_name": "Jordan",
                    "last_name": "Field",
                    "email": "jordan.field@example.test",
                    "stage": "first_interview",
                    "applied_at": "2026-04-10",
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
    if job_id:
        params["job_id"] = job_id
    if stage:
        params["stage"] = stage
    body = await eh_get(org_path("/applicants"), params=params)
    if "error" in body:
        return body
    return {"applicants": body.get("data") or body.get("items") or []}


@custom_function()
async def get_employmenthero_applicant(applicant_id: str, mock: bool = True) -> dict:
    if mock:
        return {
            "id": str(applicant_id),
            "job_id": "job-1",
            "first_name": "Jordan",
            "last_name": "Field",
            "stage": "first_interview",
        }
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get,
        org_path,
        _org_id_or_error,
    )

    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/applicants/{applicant_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def list_employmenthero_offers(job_id: str | None = None, mock: bool = True) -> dict:
    if mock:
        return {
            "offers": [
                {
                    "id": "off-1",
                    "job_id": "job-1",
                    "applicant_id": "app-1",
                    "status": "extended",
                    "extended_at": "2026-04-26",
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
    if job_id:
        params["job_id"] = job_id
    body = await eh_get(org_path("/offers"), params=params)
    if "error" in body:
        return body
    return {"offers": body.get("data") or body.get("items") or []}


@custom_function()
async def list_employmenthero_interview_stages(mock: bool = True) -> dict:
    if mock:
        return {
            "stages": [
                {"id": "stage-1", "name": "Phone Screen", "order": 1},
                {"id": "stage-2", "name": "First Interview", "order": 2},
                {"id": "stage-3", "name": "Final Interview", "order": 3},
                {"id": "stage-4", "name": "Offer", "order": 4},
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
    body = await eh_get(org_path("/interview_stages"))
    if "error" in body:
        return body
    return {"stages": body.get("data") or body.get("items") or []}


@custom_function()
async def sync_employmenthero_recruitment(mock: bool = False, since: str | None = None) -> dict:
    """Snapshot jobs, applicants, offers, interview stages.

    Applicants are filtered to those updated within the last
    ``EMPLOYMENTHERO_RECRUITMENT_RETENTION_DAYS`` (default 180) and PII
    is redacted unless ``EMPLOYMENTHERO_RECRUITMENT_REDACT_PII=false``.
    """
    import datetime as _dt
    import hashlib

    schema_version = "employment-hero.recruitment.snapshot.v1"
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
                "jobs": [
                    {
                        "id": "job-1",
                        "title": "Maintenance Operative — Camden",
                        "status": "open",
                        "posted_at": "2026-04-01",
                        "closes_at": "2026-05-15",
                        "location_id": "loc-mock-2",
                        "updated_at": started,
                    }
                ],
                "applicants": [
                    {
                        "id": "app-1",
                        "job_id": "job-1",
                        "stage": "first_interview",
                        "applied_at": "2026-04-10",
                        "first_name_length": 6,
                        "first_name_hash": "1111aaaa",
                        "last_name_length": 5,
                        "last_name_hash": "2222bbbb",
                        "email_domain": "example.test",
                        "updated_at": started,
                    }
                ],
                "offers": [
                    {
                        "id": "off-1",
                        "job_id": "job-1",
                        "applicant_id": "app-1",
                        "status": "extended",
                        "extended_at": "2026-04-26",
                        "updated_at": started,
                    }
                ],
                "interview_stages": [
                    {
                        "id": "stage-1",
                        "name": "Phone Screen",
                        "order": 1,
                        "updated_at": started,
                    }
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "recruitment",
                "started_at": started,
                "mode": "mock",
                "redacted_fields": [
                    "applicants.first_name",
                    "applicants.last_name",
                    "applicants.email",
                    "applicants.phone",
                ],
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
    page_size = cfg["api_page_size"]
    max_pages = cfg["max_pages_per_sync"]
    redact_pii = cfg["recruitment_redact_pii"]
    retention = cfg["recruitment_retention_days"]
    include_rejected = cfg["recruitment_include_rejected"]

    cutoff = (
        _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(days=retention)
    ).isoformat()

    jobs_raw = await eh_paginate(
        org_path("/jobs"),
        page_size=page_size,
        max_pages=max_pages,
    )
    jobs = [
        {
            "id": j.get("id"),
            "title": j.get("title"),
            "status": j.get("status"),
            "posted_at": j.get("posted_at"),
            "closes_at": j.get("closes_at"),
            "location_id": j.get("location_id"),
            "team_id": j.get("team_id"),
            "updated_at": j.get("updated_at"),
        }
        for j in jobs_raw
    ]

    app_params: dict = {"updated_since": since or cutoff}
    apps_raw = await eh_paginate(
        org_path("/applicants"),
        params=app_params,
        page_size=page_size,
        max_pages=max_pages,
    )
    applicants: list[dict] = []
    for a in apps_raw:
        if not include_rejected and a.get("stage") == "rejected":
            continue
        row = {
            "id": a.get("id"),
            "job_id": a.get("job_id"),
            "stage": a.get("stage"),
            "applied_at": a.get("applied_at"),
            "updated_at": a.get("updated_at"),
        }
        if redact_pii:
            fn = _redact(a.get("first_name"))
            ln = _redact(a.get("last_name"))
            row.update(
                {
                    "first_name_length": fn["length"],
                    "first_name_hash": fn["hash"],
                    "last_name_length": ln["length"],
                    "last_name_hash": ln["hash"],
                    "email_domain": (a.get("email") or "").split("@")[-1] or None,
                }
            )
        else:
            row["first_name"] = a.get("first_name")
            row["last_name"] = a.get("last_name")
            row["email"] = a.get("email")
            row["phone"] = a.get("phone")
        applicants.append(row)

    o_params: dict = {}
    if since:
        o_params["updated_since"] = since
    offers_raw = await eh_paginate(
        org_path("/offers"),
        params=o_params,
        page_size=page_size,
        max_pages=max_pages,
    )
    offers = [
        {
            "id": o.get("id"),
            "job_id": o.get("job_id"),
            "applicant_id": o.get("applicant_id"),
            "status": o.get("status"),
            "extended_at": o.get("extended_at"),
            "responded_at": o.get("responded_at"),
            "updated_at": o.get("updated_at"),
        }
        for o in offers_raw
    ]

    stages_raw = await eh_paginate(
        org_path("/interview_stages"),
        page_size=page_size,
        max_pages=max_pages,
    )
    stages = [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "order": s.get("order"),
            "updated_at": s.get("updated_at"),
        }
        for s in stages_raw
    ]

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {
            "jobs": jobs,
            "applicants": applicants,
            "offers": offers,
            "interview_stages": stages,
        },
        "metadata": {
            "integration": "employment_hero",
            "object_type": "recruitment",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "retention_days": retention,
            "redacted_fields": (
                [
                    "applicants.first_name",
                    "applicants.last_name",
                    "applicants.email",
                    "applicants.phone",
                ]
                if redact_pii
                else []
            ),
            "row_counts": {
                "jobs": len(jobs),
                "applicants": len(applicants),
                "offers": len(offers),
                "interview_stages": len(stages),
            },
        },
    }
