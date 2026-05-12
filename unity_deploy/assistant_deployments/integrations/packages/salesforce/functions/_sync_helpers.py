"""Sync-orchestrator helpers + per-object field specs for the Salesforce package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file (it's library code, not a registered tool).  All sibling
files import from here inside their function bodies to satisfy
FunctionManager's isolation rule — registered modules are not allowed
to reference module-level names within their function bodies, so all
shared constants and shared helpers live here.

Provides:

* ``OBJECT_FIELDS`` — per-sObject field lists used by ``list_*``,
  ``get_*``, and ``sync_*`` functions.  Single source of truth so
  adding a field to the mirror is a one-line change.
* ``load_sync_state`` / ``load_sync_state_rows`` / ``load_latest_run`` —
  read the persistent watermark + audit rows from DataManager.
* ``get_state_watermark`` — pluck a single ``last_synced_at`` from the
  state map.
* ``seconds_since`` — pure ISO-8601 delta.
* ``build_incremental_soql`` — construct ``WHERE SystemModstamp >= :since``
  + ``ORDER BY SystemModstamp ASC LIMIT N`` for any sObject.
* ``flatten_record`` — strip Salesforce ``attributes`` envelope and
  snake-case the keys.
* ``flatten_with_lower_email`` — flatten and lower-case the ``email``
  field for cross-app join keys.
* ``query_local_table`` — DataManager-filter wrapper used by all
  ``query_local_salesforce_*`` functions.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Per-sObject field specs
# ---------------------------------------------------------------------------

OBJECT_FIELDS: dict[str, list[str]] = {
    "Account": [
        "Id",
        "Name",
        "Type",
        "Industry",
        "Phone",
        "Website",
        "BillingStreet",
        "BillingCity",
        "BillingState",
        "BillingPostalCode",
        "BillingCountry",
        "AnnualRevenue",
        "NumberOfEmployees",
        "OwnerId",
        "Description",
        "ParentId",
        "AccountSource",
        "CreatedDate",
        "LastModifiedDate",
        "SystemModstamp",
    ],
    "Contact": [
        "Id",
        "FirstName",
        "LastName",
        "Name",
        "Email",
        "Phone",
        "MobilePhone",
        "Title",
        "Department",
        "AccountId",
        "OwnerId",
        "ReportsToId",
        "MailingStreet",
        "MailingCity",
        "MailingState",
        "MailingPostalCode",
        "MailingCountry",
        "LeadSource",
        "Description",
        "CreatedDate",
        "LastModifiedDate",
        "SystemModstamp",
    ],
    "Lead": [
        "Id",
        "FirstName",
        "LastName",
        "Name",
        "Email",
        "Phone",
        "MobilePhone",
        "Title",
        "Company",
        "Status",
        "LeadSource",
        "Industry",
        "AnnualRevenue",
        "NumberOfEmployees",
        "Rating",
        "IsConverted",
        "ConvertedAccountId",
        "ConvertedContactId",
        "ConvertedOpportunityId",
        "ConvertedDate",
        "OwnerId",
        "Country",
        "City",
        "State",
        "CreatedDate",
        "LastModifiedDate",
        "SystemModstamp",
    ],
    "Opportunity": [
        "Id",
        "Name",
        "AccountId",
        "OwnerId",
        "Amount",
        "StageName",
        "Probability",
        "CloseDate",
        "Type",
        "LeadSource",
        "IsClosed",
        "IsWon",
        "ForecastCategory",
        "ForecastCategoryName",
        "NextStep",
        "Description",
        "CreatedDate",
        "LastModifiedDate",
        "SystemModstamp",
    ],
    "Case": [
        "Id",
        "CaseNumber",
        "Subject",
        "Description",
        "Status",
        "Priority",
        "Origin",
        "Type",
        "Reason",
        "AccountId",
        "ContactId",
        "OwnerId",
        "IsClosed",
        "ClosedDate",
        "IsEscalated",
        "SuppliedName",
        "SuppliedEmail",
        "SuppliedPhone",
        "CreatedDate",
        "LastModifiedDate",
        "SystemModstamp",
    ],
}


# ---------------------------------------------------------------------------
# Sync state + audit reads
# ---------------------------------------------------------------------------


async def load_sync_state() -> dict:
    """Read the per-object watermark map from DataManager.  Returns an
    empty dict on the first run before any state row has been written.
    """
    from unity.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        rows = await dm.filter("Salesforce/Meta/SyncState", limit=200)
    except Exception:
        return {}
    return {r["object_type"]: r for r in (rows or []) if r.get("object_type")}


async def load_sync_state_rows() -> list:
    """Same as ``load_sync_state`` but returns the raw row list (stable
    shape for the public ``get_salesforce_sync_state``)."""
    from unity.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        return await dm.filter("Salesforce/Meta/SyncState", limit=200)
    except Exception:
        return []


async def load_latest_run() -> dict | None:
    """Most recent ``sync_runs`` row, or ``None`` when no runs have been
    recorded yet."""
    from unity.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        rows = await dm.filter(
            "Salesforce/Meta/SyncRuns",
            limit=1,
            order_by="started_at desc",
        )
    except Exception:
        return None
    return (rows or [None])[0]


def get_state_watermark(state: dict, object_key: str) -> str | None:
    """Pull the ``last_synced_at`` timestamp for an object type."""
    row = state.get(object_key) if state else None
    if not row:
        return None
    return row.get("last_synced_at")


def seconds_since(iso_ts: str | None) -> int | None:
    """Return seconds since an ISO-8601 timestamp; ``None`` if no input
    or unparseable input."""
    if not iso_ts:
        return None
    import datetime as _dt

    try:
        parsed = _dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    delta = _dt.datetime.now(tz=_dt.timezone.utc) - parsed
    return int(delta.total_seconds())


# ---------------------------------------------------------------------------
# SOQL + record helpers
# ---------------------------------------------------------------------------


def build_incremental_soql(
    sobject: str,
    fields: list[str],
    *,
    since: str | None,
    limit: int,
) -> str:
    """Build a SOQL string that selects ``fields`` from ``sobject``,
    filtered by ``SystemModstamp >= :since`` if a watermark is supplied,
    ordered by ``SystemModstamp ASC`` for deterministic pagination.

    ``since`` should be ISO-8601 with timezone (Salesforce accepts e.g.
    ``2026-05-06T09:00:00Z`` or ``2026-05-06T09:00:00+00:00``).  Caller
    is responsible for producing a value Salesforce will accept; we do
    not attempt to coerce here.
    """
    fields_clause = ", ".join(fields)
    where = ""
    if since:
        # SOQL date literals are not quoted.  Salesforce accepts the
        # ISO-8601 form without quotes when used after a comparison
        # operator on a datetime field.
        where = f" WHERE SystemModstamp >= {since}"
    return (
        f"SELECT {fields_clause} FROM {sobject}{where} "
        f"ORDER BY SystemModstamp ASC LIMIT {int(limit)}"
    )


def flatten_record(rec: dict) -> dict:
    """Remove Salesforce's ``attributes`` envelope and snake-case the
    top-level keys.  Nested dicts (relationships) are left as-is for
    callers that need them; per-object modules typically don't pull
    relationship fields in v0.
    """
    out: dict = {}
    for k, v in rec.items():
        if k == "attributes":
            continue
        out[_camel_to_snake(k)] = v
    return out


def flatten_with_lower_email(rec: dict) -> dict:
    """Flatten a Salesforce record and lower-case the ``email`` field
    so it joins reliably with HubSpot / Webex rows that key on the
    same address."""
    flat = flatten_record(rec)
    email = flat.get("email")
    if isinstance(email, str):
        flat["email"] = email.lower() or None
    return flat


def _camel_to_snake(name: str) -> str:
    """``LastModifiedDate`` -> ``last_modified_date``;
    ``Id`` -> ``id``; ``OwnerId`` -> ``owner_id``.
    Pure transformation, no allocations beyond the result."""
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


# ---------------------------------------------------------------------------
# Local DataManager query
# ---------------------------------------------------------------------------


async def query_local_table(
    context: str,
    *,
    limit: int,
    equality_filters: dict | None,
    order_by: str,
    mock: bool,
    mock_rows: list[dict],
) -> dict:
    """Shared DataManager-filter wrapper used by all
    ``query_local_salesforce_*`` functions."""
    if mock:
        rows = list(mock_rows)
        if equality_filters:
            rows = [
                r
                for r in rows
                if all(r.get(k) == v for k, v in equality_filters.items())
            ]
        return {
            "rows": rows[:limit],
            "count": min(len(rows), limit),
            "context": context,
            "_mock": True,
        }

    from unity.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    kwargs: dict = {"limit": limit, "order_by": order_by}
    if equality_filters:
        kwargs["filters"] = equality_filters
    try:
        rows = await dm.filter(context, **kwargs)
    except Exception as e:  # noqa: BLE001
        return {
            "error": f"DataManager filter on {context} failed: {e!r}",
            "context": context,
        }
    rows = rows or []
    return {"rows": rows, "count": len(rows), "context": context}
