"""Salesforce leads sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_salesforce_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_salesforce_leads(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot Leads into the canonical envelope."""
    import datetime as _dt

    schema_version = "salesforce.leads.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        leads = [
            {
                "id": "00Q000000000001",
                "first_name": "Pat",
                "last_name": "Prospect",
                "name": "Pat Prospect",
                "email": "pat@prospect.test",
                "company": "Prospect Co",
                "status": "Working - Contacted",
                "is_converted": False,
                "owner_id": "005000000000001",
                "system_modstamp": "2026-05-06T08:30:00Z",
                "updated_at": started,
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {"salesforce_leads": leads},
            "metadata": {
                "integration": "salesforce",
                "object_type": "leads",
                "started_at": started,
                "mode": "mock",
            },
        }

    from unify_deploy.assistant_deployments.integrations.packages.salesforce.functions._client import (
        salesforce_query,
    )
    from unify_deploy.assistant_deployments.integrations.packages.salesforce.functions._config import (
        get_salesforce_config,
    )
    from unify_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_helpers import (
        OBJECT_FIELDS,
        build_incremental_soql,
        flatten_with_lower_email,
    )

    cfg = get_salesforce_config()
    soql = build_incremental_soql(
        "Lead",
        OBJECT_FIELDS["Lead"],
        since=since,
        limit=cfg["api_page_size"],
    )
    body = await salesforce_query(soql)
    if "error" in body and not body.get("records"):
        body.update({"schema_version": schema_version, "tables": {}})
        return body

    rows = [flatten_with_lower_email(r) for r in body.get("records", [])]
    for r in rows:
        r["updated_at"] = started

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    return {
        "schema_version": schema_version,
        "tables": {"salesforce_leads": rows},
        "metadata": {
            "integration": "salesforce",
            "object_type": "leads",
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {"salesforce_leads": len(rows)},
            "pages": body.get("pages"),
            "done": body.get("done"),
        },
    }
