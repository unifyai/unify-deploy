"""Salesforce cases sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_salesforce_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_salesforce_cases(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot Cases into the canonical envelope."""
    import datetime as _dt

    schema_version = "salesforce.cases.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        cases = [
            {
                "id": "500000000000001",
                "case_number": "00001234",
                "subject": "Login timeout on Acme tenant",
                "status": "Working",
                "priority": "High",
                "account_id": "001000000000001",
                "contact_id": "003000000000001",
                "owner_id": "005000000000001",
                "is_closed": False,
                "system_modstamp": "2026-05-06T07:45:00Z",
                "updated_at": started,
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {"salesforce_cases": cases},
            "metadata": {
                "integration": "salesforce",
                "object_type": "cases",
                "started_at": started,
                "mode": "mock",
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.salesforce.functions._client import (
        salesforce_query,
    )
    from droid_deploy.assistant_deployments.integrations.packages.salesforce.functions._config import (
        get_salesforce_config,
    )
    from droid_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_helpers import (
        OBJECT_FIELDS,
        build_incremental_soql,
        flatten_record,
    )

    cfg = get_salesforce_config()
    soql = build_incremental_soql(
        "Case",
        OBJECT_FIELDS["Case"],
        since=since,
        limit=cfg["api_page_size"],
    )
    body = await salesforce_query(soql)
    if "error" in body and not body.get("records"):
        body.update({"schema_version": schema_version, "tables": {}})
        return body

    rows = [flatten_record(r) for r in body.get("records", [])]
    for r in rows:
        r["updated_at"] = started

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"salesforce_cases": rows},
        "metadata": {
            "integration": "salesforce",
            "object_type": "cases",
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {"salesforce_cases": len(rows)},
            "pages": body.get("pages"),
            "done": body.get("done"),
        },
    }
