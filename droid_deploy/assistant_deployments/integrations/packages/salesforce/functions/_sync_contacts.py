"""Salesforce contacts sync — internal-only.

Underscore-prefixed so FunctionManager skips discovery.  Called by
the top-level ``sync.run_salesforce_sync_tick`` orchestrator via
importlib; not exposed as a registered tool.
"""

from __future__ import annotations


async def sync_salesforce_contacts(
    mock: bool = False,
    since: str | None = None,
) -> dict:
    """Snapshot Contacts into the canonical envelope."""
    import datetime as _dt

    schema_version = "salesforce.contacts.snapshot.v1"
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        contacts = [
            {
                "id": "003000000000001",
                "first_name": "Alex",
                "last_name": "Example",
                "name": "Alex Example",
                "email": "alex@example.test",
                "title": "Head of Operations",
                "account_id": "001000000000001",
                "owner_id": "005000000000001",
                "system_modstamp": "2026-05-06T09:00:00Z",
                "updated_at": started,
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": {"salesforce_contacts": contacts},
            "metadata": {
                "integration": "salesforce",
                "object_type": "contacts",
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
        flatten_with_lower_email,
    )

    cfg = get_salesforce_config()
    soql = build_incremental_soql(
        "Contact",
        OBJECT_FIELDS["Contact"],
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
        "tables": {"salesforce_contacts": rows},
        "metadata": {
            "integration": "salesforce",
            "object_type": "contacts",
            "started_at": started,
            "finished_at": finished,
            "since": since,
            "row_counts": {"salesforce_contacts": len(rows)},
            "pages": body.get("pages"),
            "done": body.get("done"),
        },
    }
