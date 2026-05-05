"""Employment Hero employee notes — confidential HR notes.

Notes are private HR records.  Snapshot stores metadata + redacted body
length/hash; full body must be fetched on-demand via the live API.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def list_employee_notes(
    employee_id: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    if mock:
        return {"notes": [
            {"id": "note-1", "employee_id": "emp-mock-1",
             "author_id": "emp-mock-3", "category": "performance",
             "created_at": "2026-04-12T10:00:00Z", "is_confidential": True,
             "body": "Discussed Q2 occupancy targets; aligned on 92%."},
        ]}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    if employee_id:
        path = org_path(f"/employees/{employee_id}/notes")
        params: dict = {"limit": min(limit, 100)}
    else:
        path = org_path("/notes")
        params = {"limit": min(limit, 100)}
    body = await eh_get(path, params=params)
    if "error" in body:
        return body
    return {"notes": body.get("data") or body.get("items") or []}


@custom_function()
async def get_employee_note(note_id: str, mock: bool = True) -> dict:
    if mock:
        return {"id": str(note_id), "employee_id": "emp-mock-1",
                "author_id": "emp-mock-3", "category": "performance",
                "created_at": "2026-04-12T10:00:00Z",
                "is_confidential": True,
                "body": "Discussed Q2 occupancy targets; aligned on 92%."}
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._client import (
        eh_get, org_path, _org_id_or_error,
    )
    _, err = _org_id_or_error()
    if err is not None:
        return err
    body = await eh_get(org_path(f"/notes/{note_id}"))
    if "error" in body:
        return body
    return body.get("data") or body


@custom_function()
async def create_employee_note(
    employee_id: str,
    body: str,
    category: str | None = None,
    is_confidential: bool = True,
    confirm: bool = False,
    mock: bool = True,
) -> dict:
    """Create an HR note against an employee.  HIGH-STAKES WRITE."""
    if mock:
        return {"id": "note-mock-new", "employee_id": employee_id,
                "category": category, "is_confidential": is_confidential,
                "_mocked": True}

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
    payload: dict = {
        "employee_id": employee_id,
        "body": body,
        "is_confidential": is_confidential,
    }
    if category:
        payload["category"] = category
    return await eh_post(org_path("/notes"), payload)


@custom_function()
async def sync_employee_notes(mock: bool = False, since: str | None = None) -> dict:
    """Snapshot employee notes — body redacted to length + hash."""
    import datetime as _dt
    import hashlib

    schema_version = "employment-hero.employee-notes.snapshot.v1"
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
            "tables": {"employee_notes": [{
                "id": "note-1", "employee_id": "emp-mock-1",
                "author_id": "emp-mock-3", "category": "performance",
                "is_confidential": True,
                "body_redacted_length": 56, "body_redacted_hash": "9876fedc",
                "created_at": "2026-04-12T10:00:00Z",
                "updated_at": started,
            }]},
            "metadata": {"integration": "employment_hero",
                         "object_type": "employee_notes",
                         "started_at": started, "mode": "mock",
                         "redacted_fields": ["body"]},
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

    params: dict = {}
    if since:
        params["updated_since"] = since
    raw = await eh_paginate(
        org_path("/notes"),
        params=params,
        page_size=cfg["api_page_size"],
        max_pages=cfg["max_pages_per_sync"],
    )
    employee_notes: list[dict] = []
    for n in raw:
        body_red = _redact(n.get("body"))
        employee_notes.append({
            "id": n.get("id"),
            "employee_id": n.get("employee_id"),
            "author_id": n.get("author_id"),
            "category": n.get("category"),
            "is_confidential": n.get("is_confidential"),
            "body_redacted_length": body_red["length"],
            "body_redacted_hash": body_red["hash"],
            "created_at": n.get("created_at"),
            "updated_at": n.get("updated_at"),
        })

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"employee_notes": employee_notes},
        "metadata": {
            "integration": "employment_hero", "object_type": "employee_notes",
            "organisation_id": org_id,
            "started_at": started, "finished_at": finished, "since": since,
            "redacted_fields": ["body"],
            "row_counts": {"employee_notes": len(employee_notes)},
        },
    }
