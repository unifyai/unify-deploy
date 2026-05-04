"""Local DataManager query helpers - read the synced HubSpot copy.

Prefer these over live ``get_*``/``search_*`` calls when the data's
freshness allows.  See ``guidance/local_vs_live.md`` for the heuristic.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


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
            "results": [
                {
                    "hubspot_id": "12345",
                    "email": "sample@example.com",
                    "firstname": "Sample",
                    "lastname": "Contact",
                    "lifecyclestage": "lead",
                },
            ],
            "freshness": {
                "last_synced_at": "2026-04-26T15:00:00Z",
                "is_fresh": True,
                "threshold_seconds": 3600,
            },
            "source": "local_mock",
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_helpers import (
        build_filter,
        local_query_contexts,
    )

    contexts = local_query_contexts()
    filt = build_filter(
        [
            ("email", "==", email),
            ("lifecyclestage", "==", lifecycle_stage),
        ],
    )

    if name_query:
        rows = await primitives.data.search(  # noqa: F821
            contexts["contacts"],
            references={"firstname": name_query, "lastname": name_query},
            k=limit,
            filter=filt or None,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            contexts["contacts"],
            filter=filt or None,
            limit=limit,
        )
    freshness = await _freshness_inline("contacts")
    return {
        "results": rows or [],
        "freshness": freshness,
        "source": "local",
    }


@custom_function()
async def query_local_companies(
    name_query: str | None = None,
    domain: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Query the synced HubSpot companies copy in DataManager."""
    if mock:
        return {
            "results": [
                {
                    "hubspot_id": "5001",
                    "name": "Acme Properties LLC",
                    "domain": "acme-properties.com",
                    "industry": "REAL_ESTATE",
                },
            ],
            "freshness": {
                "last_synced_at": "2026-04-26T15:00:00Z",
                "is_fresh": True,
                "threshold_seconds": 3600,
            },
            "source": "local_mock",
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_helpers import (
        build_filter,
        local_query_contexts,
    )

    contexts = local_query_contexts()
    filt = build_filter([("domain", "==", domain)])
    if name_query:
        rows = await primitives.data.search(  # noqa: F821
            contexts["companies"],
            references={"name": name_query},
            k=limit,
            filter=filt or None,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            contexts["companies"],
            filter=filt or None,
            limit=limit,
        )
    freshness = await _freshness_inline("companies")
    return {
        "results": rows or [],
        "freshness": freshness,
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
    """Query the synced HubSpot deals copy in DataManager."""
    if mock:
        return {
            "results": [
                {
                    "hubspot_id": "9001",
                    "dealname": "Acme - Q3 Mgmt Contract",
                    "dealstage": "presentationscheduled",
                    "pipeline": "default",
                    "amount": "120000",
                    "hubspot_owner_id": "60001",
                },
            ],
            "freshness": {
                "last_synced_at": "2026-04-26T15:00:00Z",
                "is_fresh": True,
                "threshold_seconds": 3600,
            },
            "source": "local_mock",
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_helpers import (
        build_filter,
        local_query_contexts,
    )

    contexts = local_query_contexts()
    filt = build_filter(
        [
            ("dealstage", "==", stage),
            ("pipeline", "==", pipeline),
            ("hubspot_owner_id", "==", owner_id),
        ],
    )
    if name_query:
        rows = await primitives.data.search(  # noqa: F821
            contexts["deals"],
            references={"dealname": name_query},
            k=limit,
            filter=filt or None,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            contexts["deals"],
            filter=filt or None,
            limit=limit,
        )
    freshness = await _freshness_inline("deals")
    return {
        "results": rows or [],
        "freshness": freshness,
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
    """Query the synced HubSpot tickets copy in DataManager."""
    if mock:
        return {
            "results": [
                {
                    "hubspot_id": "8001",
                    "subject": "Maintenance: leaky faucet in unit 3B",
                    "hs_pipeline_stage": "1",
                    "hs_ticket_priority": "MEDIUM",
                },
            ],
            "freshness": {
                "last_synced_at": "2026-04-26T15:00:00Z",
                "is_fresh": True,
                "threshold_seconds": 3600,
            },
            "source": "local_mock",
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_helpers import (
        build_filter,
        local_query_contexts,
    )

    contexts = local_query_contexts()
    filt = build_filter(
        [
            ("hs_pipeline_stage", "==", pipeline_stage),
            ("hs_ticket_priority", "==", priority),
        ],
    )
    if subject_query:
        rows = await primitives.data.search(  # noqa: F821
            contexts["tickets"],
            references={"subject": subject_query, "content": subject_query},
            k=limit,
            filter=filt or None,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            contexts["tickets"],
            filter=filt or None,
            limit=limit,
        )
    freshness = await _freshness_inline("tickets")
    return {
        "results": rows or [],
        "freshness": freshness,
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
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_helpers import (
        build_filter,
        engagement_body_columns,
        engagement_contexts,
    )

    eng_contexts = engagement_contexts()
    if engagement_type not in eng_contexts:
        return {
            "error": f"Unknown engagement_type '{engagement_type}'.  "
            f"Allowed: {sorted(eng_contexts)}",
        }
    ctx = eng_contexts[engagement_type]

    if mock:
        return {
            "engagement_type": engagement_type,
            "results": [
                {
                    "hubspot_id": "N1001",
                    "object_type": f"engagement_{engagement_type}",
                },
            ],
            "freshness": {
                "last_synced_at": "2026-04-26T15:00:00Z",
                "is_fresh": True,
                "threshold_seconds": 3600,
            },
            "source": "local_mock",
        }

    filt = build_filter([("hubspot_owner_id", "==", owner_id)])
    if body_query:
        body_columns = engagement_body_columns()
        ref_col = body_columns[engagement_type]
        rows = await primitives.data.search(  # noqa: F821
            ctx,
            references={ref_col: body_query},
            k=limit,
            filter=filt or None,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            ctx,
            filter=filt or None,
            limit=limit,
        )
    freshness = await _freshness_inline(engagement_type + "s")
    return {
        "engagement_type": engagement_type,
        "results": rows or [],
        "freshness": freshness,
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
            "freshness": {
                "last_synced_at": "2026-04-26T15:00:00Z",
                "is_fresh": True,
                "threshold_seconds": 3600,
            },
            "source": "local_mock",
        }

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_helpers import (
        local_query_contexts,
    )

    contexts = local_query_contexts()
    ctx = f"{contexts['custom_objects']}/{object_type}/Records"
    if name_query:
        rows = await primitives.data.search(  # noqa: F821
            ctx,
            references={"name": name_query},
            k=limit,
        )
    else:
        rows = await primitives.data.filter(  # noqa: F821
            ctx,
            limit=limit,
        )
    freshness = await _freshness_inline("custom_objects")
    return {
        "object_type": object_type,
        "results": rows or [],
        "freshness": freshness,
        "source": "local",
    }


@custom_function()
async def _freshness_inline(object_key: str) -> dict:
    """Look up the latest sync_state row for ``object_key`` and decide if
    it's fresh enough.  Internal helper - decorated only because the
    compliance test requires every top-level def to have @custom_function."""
    import os

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_helpers import (
        local_query_contexts,
        seconds_since,
    )

    contexts = local_query_contexts()
    threshold = int(os.environ.get("HUBSPOT_LOCAL_FRESHNESS_THRESHOLD_SECONDS", "3600"))
    try:
        rows = await primitives.data.filter(  # noqa: F821
            contexts["sync_state"],
            filter=f"`object_type` == {object_key!r}",
            limit=1,
        )
    except Exception:
        return {
            "last_synced_at": None,
            "is_fresh": False,
            "threshold_seconds": threshold,
        }
    if not rows:
        return {
            "last_synced_at": None,
            "is_fresh": False,
            "threshold_seconds": threshold,
        }
    last = rows[0].get("last_synced_at")
    secs = seconds_since(last)
    is_fresh = secs is not None and secs < threshold
    return {
        "last_synced_at": last,
        "is_fresh": is_fresh,
        "threshold_seconds": threshold,
    }
