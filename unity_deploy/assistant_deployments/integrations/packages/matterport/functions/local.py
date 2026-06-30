"""Local DataManager query helpers for Matterport.

After ``run_matterport_sync_tick`` has materialised data into
``Matterport/...`` contexts, these functions are the preferred read
path for analytical queries.  Each returns a ``freshness`` block so the
assistant knows whether to trust the local copy.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
async def query_local_matterport_models(
    address_query: str | None = None,
    unit_id: str | None = None,
    status: str | None = None,
    limit: int = 25,
    mock: bool = True,
) -> dict:
    """Query the synced models table.

    ``unit_id`` filters via the ``Matterport/Links/ModelUnit`` table —
    returns models linked to that RealPage unit.
    """
    if mock:
        return {
            "rows": [
                {
                    "model_id": "mdl-mock-1",
                    "name": "401 Westwood Ave - Unit 4B",
                    "internal_label": "unit:4B",
                    "city": "Denver",
                    "postal_code": "80205",
                    "status": "processed",
                },
            ],
            "count": 1,
            "freshness": {
                "last_synced_at": "2026-04-30T02:00:00Z",
                "is_fresh": True,
                "threshold_seconds": 172_800,
            },
        }

    from unify.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()

    if unit_id:
        safe = str(unit_id).replace("'", "''")
        link_rows = await safe_filter(
            dm,
            "Matterport/Links/ModelUnit",
            filter=f"`unit_id` == '{safe}'",
            limit=limit,
        )
        model_ids = [r.get("model_id") for r in link_rows if r.get("model_id")]
        if not model_ids:
            return {"rows": [], "count": 0, "freshness": await freshness("models")}
        ids_quoted = ", ".join(f"'{m}'" for m in model_ids)
        rows = await safe_filter(
            dm,
            "Matterport/Models",
            filter=f"`model_id` IN ({ids_quoted})",
            limit=limit,
        )
        return {
            "rows": rows,
            "count": len(rows),
            "freshness": await freshness("models"),
        }

    filters: list[str] = []
    if status:
        safe = status.replace("'", "''")
        filters.append(f"`status` == '{safe}'")
    if address_query:
        safe = address_query.replace("'", "''")
        filters.append(
            f"(`address_line1` LIKE '%{safe}%' OR `city` LIKE '%{safe}%' OR `postal_code` LIKE '%{safe}%')",
        )
    rows = await safe_filter(
        dm,
        "Matterport/Models",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {"rows": rows, "count": len(rows), "freshness": await freshness("models")}


@custom_function()
async def query_local_matterport_view_stats(
    model_id: str | None = None,
    unit_id: str | None = None,
    since: str | None = None,
    limit: int = 100,
    mock: bool = True,
) -> dict:
    """Query the synced ``view_stats_daily`` table.

    Pass ``unit_id`` to roll up across all models linked to that RealPage
    unit (sums per day across linked models).
    """
    if mock:
        return {
            "rows": [
                {
                    "model_id": "mdl-mock-1",
                    "day": "2026-04-29",
                    "total_views": 31,
                    "unique_visitors": 25,
                    "avg_seconds": 198,
                    "top_referrer": "hubspot.acme.com/listing/4b",
                },
            ],
            "count": 1,
            "freshness": {
                "last_synced_at": "2026-04-30T09:00:00Z",
                "is_fresh": True,
                "threshold_seconds": 7_200,
            },
        }

    from unify.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.matterport.functions._local_helpers import (
        freshness,
        safe_filter,
    )

    dm = ManagerRegistry.get_data_manager()

    target_model_ids: list[str] = []
    if unit_id:
        safe = str(unit_id).replace("'", "''")
        link_rows = await safe_filter(
            dm,
            "Matterport/Links/ModelUnit",
            filter=f"`unit_id` == '{safe}'",
            limit=50,
        )
        target_model_ids = [r.get("model_id") for r in link_rows if r.get("model_id")]
        if not target_model_ids:
            return {"rows": [], "count": 0, "freshness": await freshness("view_stats")}
    elif model_id:
        target_model_ids = [str(model_id)]

    filters: list[str] = []
    if target_model_ids:
        ids_quoted = ", ".join(f"'{m}'" for m in target_model_ids)
        filters.append(f"`model_id` IN ({ids_quoted})")
    if since:
        safe = since.replace("'", "''")
        filters.append(f"`day` >= '{safe}'")

    rows = await safe_filter(
        dm,
        "Matterport/ViewStats/Daily",
        filter=" AND ".join(filters) if filters else None,
        limit=limit,
    )
    return {
        "rows": rows,
        "count": len(rows),
        "freshness": await freshness("view_stats"),
    }
