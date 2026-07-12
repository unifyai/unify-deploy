"""Salesforce accounts sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_salesforce_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_salesforce_accounts(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot Accounts into the canonical envelope.

    ``since`` is an ISO-8601 timestamp; when set, only Accounts with
    ``SystemModstamp >= since`` are pulled.  Otherwise pulls everything
    up to the configured page cap.
    """
    import datetime as _dt

    schema_version = "salesforce.accounts.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        accounts = [
            {
                "id": "001000000000001",
                "name": "Acme Corp",
                "type": "Customer - Direct",
                "industry": "Technology",
                "owner_id": "005000000000001",
                "annual_revenue": 5_000_000,
                "number_of_employees": 42,
                "billing_country": "US",
                "system_modstamp": "2026-05-06T09:00:00Z",
                "updated_at": started,
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {"salesforce_accounts": accounts},
            "metadata": {
                "integration": "salesforce",
                "object_type": "accounts",
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
        flatten_record,
    )

    cfg = get_salesforce_config()
    soql = build_incremental_soql(
        "Account",
        OBJECT_FIELDS["Account"],
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
        "tables": {"salesforce_accounts": rows},
        "metadata": {
            "integration": "salesforce",
            "object_type": "accounts",
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {"salesforce_accounts": len(rows)},
            "pages": body.get("pages"),
            "done": body.get("done"),
        },
    }
