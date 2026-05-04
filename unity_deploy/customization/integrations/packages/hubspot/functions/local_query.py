"""Local DataManager query helpers - read the synced HubSpot copy.

Prefer these over live ``get_*``/``search_*`` calls when the data's
freshness allows.  See ``guidance/local_vs_live.md`` for the heuristic.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


_CONTACTS_CTX = "HubSpot/CRM/Dimensions/Contacts"
_COMPANIES_CTX = "HubSpot/CRM/Dimensions/Companies"
_DEALS_CTX = "HubSpot/CRM/Dimensions/Deals"
_TICKETS_CTX = "HubSpot/CRM/Dimensions/Tickets"
_CUSTOM_OBJECTS_CTX = "HubSpot/CRM/CustomObjects"
_ENGAGEMENT_CTX = {
    "call": "HubSpot/CRM/Engagements/Calls",
    "email": "HubSpot/CRM/Engagements/Emails",
    "meeting": "HubSpot/CRM/Engagements/Meetings",
    "note": "HubSpot/CRM/Engagements/Notes",
    "task": "HubSpot/CRM/Engagements/Tasks",
}
_SYNC_STATE_CTX = "HubSpot/CRM/Meta/SyncState"


@custom_function()
async def query_local_contacts(
    email: str | None = None,
    name_query: str | None = None,
    lifecycle_stage: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Query the synced HubSpot contacts copy in DataManager."""
    if mock:
        return {
            "results": [{
                "hubspot_id": "12345", "email": "sample@example.com",
                "firstname": "Sample", "lastname": "Contact",
                "lifecyclestage": "lead",
            }],
            "freshness": _mock_freshness("contacts"),
            "source": "local_mock",
        }

    filt = _build_filter([
        ("email", "==", email),
        ("lifecyclestage", "==", lifecycle_stage),
    ])

    if name_query:
        rows = await primitives.data.search(  # noqa: F821
            _CONTACTS_CTX,
            references={"firstname": name_query, "lastname": name_query},
            k=limit,
            filter=filt or None,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            _CONTACTS_CTX,
            filter=filt or None,
            limit=limit,
        )
    return {
        "results": rows or [],
        "freshness": await _get_freshness("contacts"),
        "source": "local",
    }


@custom_function()
async def query_local_companies(
    name_query: str | None = None,
    domain: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "results": [{
                "hubspot_id": "5001", "name": "Acme Properties LLC",
                "domain": "acme-properties.com", "industry": "REAL_ESTATE",
            }],
            "freshness": _mock_freshness("companies"),
            "source": "local_mock",
        }

    filt = _build_filter([("domain", "==", domain)])
    if name_query:
        rows = await primitives.data.search(  # noqa: F821
            _COMPANIES_CTX,
            references={"name": name_query},
            k=limit, filter=filt or None,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            _COMPANIES_CTX, filter=filt or None, limit=limit,
        )
    return {
        "results": rows or [],
        "freshness": await _get_freshness("companies"),
        "source": "local",
    }


@custom_function()
async def query_local_deals(
    stage: str | None = None,
    pipeline: str | None = None,
    owner_id: str | None = None,
    name_query: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "results": [{
                "hubspot_id": "9001", "dealname": "Acme - Q3 Mgmt Contract",
                "dealstage": "presentationscheduled", "pipeline": "default",
                "amount": "120000", "hubspot_owner_id": "60001",
            }],
            "freshness": _mock_freshness("deals"),
            "source": "local_mock",
        }

    filt = _build_filter([
        ("dealstage", "==", stage),
        ("pipeline", "==", pipeline),
        ("hubspot_owner_id", "==", owner_id),
    ])
    if name_query:
        rows = await primitives.data.search(  # noqa: F821
            _DEALS_CTX,
            references={"dealname": name_query},
            k=limit, filter=filt or None,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            _DEALS_CTX, filter=filt or None, limit=limit,
        )
    return {
        "results": rows or [],
        "freshness": await _get_freshness("deals"),
        "source": "local",
    }


@custom_function()
async def query_local_tickets(
    pipeline_stage: str | None = None,
    priority: str | None = None,
    subject_query: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    if mock:
        return {
            "results": [{
                "hubspot_id": "8001",
                "subject": "Maintenance: leaky faucet in unit 3B",
                "hs_pipeline_stage": "1", "hs_ticket_priority": "MEDIUM",
            }],
            "freshness": _mock_freshness("tickets"),
            "source": "local_mock",
        }

    filt = _build_filter([
        ("hs_pipeline_stage", "==", pipeline_stage),
        ("hs_ticket_priority", "==", priority),
    ])
    if subject_query:
        rows = await primitives.data.search(  # noqa: F821
            _TICKETS_CTX,
            references={"subject": subject_query, "content": subject_query},
            k=limit, filter=filt or None,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            _TICKETS_CTX, filter=filt or None, limit=limit,
        )
    return {
        "results": rows or [],
        "freshness": await _get_freshness("tickets"),
        "source": "local",
    }


@custom_function()
async def query_local_engagements(
    engagement_type: str = "note",
    body_query: str | None = None,
    owner_id: str | None = None,
    limit: int = 50,
    mock: bool = True,
) -> dict:
    """Query synced engagements (call, email, meeting, note, task)."""
    if engagement_type not in _ENGAGEMENT_CTX:
        return {"error": f"Unknown engagement_type '{engagement_type}'.  "
                          f"Allowed: {sorted(_ENGAGEMENT_CTX)}"}
    ctx = _ENGAGEMENT_CTX[engagement_type]

    if mock:
        return {
            "engagement_type": engagement_type,
            "results": [{"hubspot_id": "N1001", "object_type": f"engagement_{engagement_type}"}],
            "freshness": _mock_freshness(engagement_type + "s"),
            "source": "local_mock",
        }

    filt = _build_filter([("hubspot_owner_id", "==", owner_id)])
    if body_query:
        ref_col = {
            "call": "hs_call_body", "email": "hs_email_text",
            "meeting": "hs_meeting_body", "note": "hs_note_body",
            "task": "hs_task_body",
        }[engagement_type]
        rows = await primitives.data.search(  # noqa: F821
            ctx, references={ref_col: body_query}, k=limit,
            filter=filt or None,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            ctx, filter=filt or None, limit=limit,
        )
    return {
        "engagement_type": engagement_type,
        "results": rows or [],
        "freshness": await _get_freshness(engagement_type + "s"),
        "source": "local",
    }


@custom_function()
async def query_local_custom_objects(
    object_type: str,
    name_query: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Query the synced custom-object records.  ``object_type`` is the
    fully-qualified HubSpot type name (e.g. ``p_<portalId>_property``)."""
    if mock:
        return {
            "object_type": object_type,
            "results": [{"hubspot_id": "11001", "name": "Sunset Tower"}],
            "freshness": _mock_freshness("custom_objects"),
            "source": "local_mock",
        }

    ctx = f"{_CUSTOM_OBJECTS_CTX}/{object_type}/Records"
    if name_query:
        rows = await primitives.data.search(  # noqa: F821
            ctx, references={"name": name_query}, k=limit,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            ctx, limit=limit,
        )
    return {
        "object_type": object_type,
        "results": rows or [],
        "freshness": await _get_freshness("custom_objects"),
        "source": "local",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_filter(pairs: list[tuple[str, str, str | None]]) -> str:
    """Build a DataManager filter expression from non-None ``(col, op, value)`` pairs."""
    parts = []
    for col, op, value in pairs:
        if value is None or value == "":
            continue
        parts.append(f"`{col}` {op} {value!r}")
    return " and ".join(parts)


async def _get_freshness(object_key: str) -> dict:
    """Look up the latest sync_state row for ``object_key`` and decide if
    it's fresh enough.  Returns ``{last_synced_at, is_fresh, threshold_seconds}``."""
    import os
    threshold = int(os.environ.get("HUBSPOT_LOCAL_FRESHNESS_THRESHOLD_SECONDS", "3600"))
    try:
        rows = await primitives.data.filter(  # noqa: F821
            _SYNC_STATE_CTX,
            filter=f"`object_type` == {object_key!r}",
            limit=1,
        )
    except Exception:
        return {"last_synced_at": None, "is_fresh": False, "threshold_seconds": threshold}
    if not rows:
        return {"last_synced_at": None, "is_fresh": False, "threshold_seconds": threshold}
    last = rows[0].get("last_synced_at")
    is_fresh = _seconds_since(last) is not None and _seconds_since(last) < threshold
    return {"last_synced_at": last, "is_fresh": is_fresh, "threshold_seconds": threshold}


def _mock_freshness(_object_key: str) -> dict:
    return {"last_synced_at": "2026-04-26T15:00:00Z", "is_fresh": True,
            "threshold_seconds": 3600}


def _seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    import datetime as _dt
    try:
        ts = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (_dt.datetime.now(tz=_dt.timezone.utc) - ts).total_seconds()
