"""Employment Hero employee_notes sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_employmenthero_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_employmenthero_employee_notes(
    mock: bool = False,
    since: str | None = None,
) -> dict:
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
            "tables": {
                "employee_notes": [
                    {
                        "id": "note-1",
                        "employee_id": "emp-mock-1",
                        "author_id": "emp-mock-3",
                        "category": "performance",
                        "is_confidential": True,
                        "body_redacted_length": 56,
                        "body_redacted_hash": "9876fedc",
                        "created_at": "2026-04-12T10:00:00Z",
                        "updated_at": started,
                    },
                ],
            },
            "metadata": {
                "integration": "employment_hero",
                "object_type": "employee_notes",
                "started_at": started,
                "mode": "mock",
                "redacted_fields": ["body"],
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
        employee_notes.append(
            {
                "id": n.get("id"),
                "employee_id": n.get("employee_id"),
                "author_id": n.get("author_id"),
                "category": n.get("category"),
                "is_confidential": n.get("is_confidential"),
                "body_redacted_length": body_red["length"],
                "body_redacted_hash": body_red["hash"],
                "created_at": n.get("created_at"),
                "updated_at": n.get("updated_at"),
            },
        )

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"employee_notes": employee_notes},
        "metadata": {
            "integration": "employment_hero",
            "object_type": "employee_notes",
            "organisation_id": org_id,
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "redacted_fields": ["body"],
            "row_counts": {"employee_notes": len(employee_notes)},
        },
    }
