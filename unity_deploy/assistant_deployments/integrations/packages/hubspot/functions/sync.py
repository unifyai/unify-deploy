"""HubSpot sync orchestrator + state.

``run_hubspot_sync_tick`` is the entrypoint the scenario runtime calls
on its interval.  It dispatches per-object sync functions, gates by
per-object cadence config, and emits an audit row.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def run_hubspot_sync_tick(full: bool = False, mock: bool = True) -> dict:
    """Run a single sync tick.

    Reads the per-object watermark from ``HubSpot/CRM/Meta/SyncState``,
    dispatches each enabled sync function, aggregates the returned tables
    into one envelope, and appends an audit row to ``SyncRuns``.

    Parameters
    ----------
    full : bool
        Ignore the watermark and pull everything.
    mock : bool
        Return a tiny synthetic envelope without touching the API.
    """
    import datetime as _dt

    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_helpers import (
        crm_sync_registry,
        engagement_sync_registry,
        marketing_sync_registry,
        sales_sync_registry,
        service_sync_registry,
        seconds_since,
    )

    cfg = get_hubspot_config()
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        # Minimal aggregated mock envelope - exercises the contract.
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_contacts import (
            sync_hubspot_contacts,
        )
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_companies import (
            sync_hubspot_companies,
        )
        from unity_deploy.assistant_deployments.integrations.packages.hubspot.functions._sync_deals import (
            sync_hubspot_deals,
        )

        contacts_env = await sync_hubspot_contacts(mock=True)
        companies_env = await sync_hubspot_companies(mock=True)
        deals_env = await sync_hubspot_deals(mock=True)

        finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        tables = {
            **contacts_env["tables"],
            **companies_env["tables"],
            **deals_env["tables"],
            "sync_state": [
                {"object_type": "contacts", "last_synced_at": finished},
                {"object_type": "companies", "last_synced_at": finished},
                {"object_type": "deals", "last_synced_at": finished},
            ],
            "sync_runs": [
                {
                    "started_at": started,
                    "finished_at": finished,
                    "row_totals_json": str(
                        {
                            "contacts": len(contacts_env["tables"]["contacts"]),
                            "companies": len(companies_env["tables"]["companies"]),
                            "deals": len(deals_env["tables"]["deals"]),
                        },
                    ),
                    "errors_json": "[]",
                    "config_snapshot_json": str(
                        {
                            k: cfg[k]
                            for k in ("sync_min_interval_seconds", "api_page_size")
                        },
                    ),
                    "mode": "mock",
                },
            ],
        }
        return {
            "schema_version": "hubspot.crm.sync.v1",
            "tables": tables,
            "metadata": {
                "started_at": started,
                "finished_at": finished,
                "mode": "mock",
            },
        }

    # ----- Real-mode dispatch -----------------------------------------------
    sync_state = await _load_hubspot_sync_state_inline()
    errors: list[dict] = []
    row_totals: dict[str, int] = {}
    aggregated_tables: dict[str, list] = {}

    crm_reg = crm_sync_registry()
    eng_reg = engagement_sync_registry()
    mkt_reg = marketing_sync_registry()
    sales_reg = sales_sync_registry()
    svc_reg = service_sync_registry()

    accepts_since_keys = {
        "contacts",
        "companies",
        "deals",
        "tickets",
        "line_items",
        "products",
        "quotes",
    }

    async def _dispatch(
        object_key: str,
        registry: dict,
        accepts_since: bool,
    ) -> None:
        if object_key not in registry:
            return
        gap = seconds_since(sync_state.get(object_key))
        min_gap = cfg["object_intervals"].get(
            object_key,
            cfg["sync_min_interval_seconds"],
        )
        if not full and gap is not None and gap < min_gap:
            return
        module_stem, fn_name = registry[object_key]
        try:
            module = __import__(
                f"unity_deploy.assistant_deployments.integrations.packages.hubspot.functions.{module_stem}",
                fromlist=[fn_name],
            )
        except ImportError as e:
            errors.append({"object_type": object_key, "error": f"import failed: {e}"})
            return
        fn = getattr(module, fn_name, None)
        if fn is None:
            errors.append(
                {"object_type": object_key, "error": f"function {fn_name} missing"},
            )
            return
        kwargs: dict = {"mock": False}
        if accepts_since and not full and sync_state.get(object_key):
            kwargs["since"] = sync_state[object_key]
        envelope = await fn(**kwargs)
        for table_name, rows in (envelope.get("tables") or {}).items():
            aggregated_tables.setdefault(table_name, []).extend(rows or [])
            row_totals[table_name] = row_totals.get(table_name, 0) + len(rows or [])
        if "error" in envelope:
            errors.append({"object_type": object_key, "error": envelope["error"]})

    # CRM
    if "crm" in cfg["sync_hubs"]:
        for k in cfg["sync_objects"]:
            await _dispatch(k, crm_reg, k in accepts_since_keys)
        if cfg["sync_custom_objects"]:
            await _dispatch("custom_objects", crm_reg, False)

    # Engagements
    if "engagements" in cfg["sync_hubs"] and cfg["sync_engagements"]:
        for k in cfg["sync_engagement_types"]:
            await _dispatch(k, eng_reg, True)

    # Marketing / Sales / Service - sync all known surfaces by default; tier
    # gating handles 403s gracefully.
    if "marketing" in cfg["sync_hubs"]:
        for k in mkt_reg:
            await _dispatch(k, mkt_reg, False)
    if "sales" in cfg["sync_hubs"]:
        for k in sales_reg:
            await _dispatch(k, sales_reg, False)
    if "service" in cfg["sync_hubs"]:
        for k in svc_reg:
            await _dispatch(k, svc_reg, False)

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    aggregated_tables["sync_state"] = [
        {"object_type": k, "last_synced_at": finished} for k in row_totals
    ]
    aggregated_tables["sync_runs"] = [
        {
            "started_at": started,
            "finished_at": finished,
            "row_totals_json": str(row_totals),
            "errors_json": str(errors),
            "config_snapshot_json": str(
                {
                    k: cfg[k]
                    for k in (
                        "sync_min_interval_seconds",
                        "api_page_size",
                        "sync_hubs",
                        "sync_objects",
                    )
                },
            ),
            "mode": "real",
        },
    ]
    return {
        "schema_version": "hubspot.crm.sync.v1",
        "tables": aggregated_tables,
        "metadata": {
            "started_at": started,
            "finished_at": finished,
            "row_totals": row_totals,
            "errors": errors,
            "mode": "real",
        },
    }


@custom_function()
async def get_hubspot_sync_state(mock: bool = True) -> dict:
    """Return the current sync watermarks per object type from DataManager."""
    if mock:
        return {
            "sync_state": [
                {"object_type": "contacts", "last_synced_at": "2026-04-26T15:00:00Z"},
                {"object_type": "companies", "last_synced_at": "2026-04-26T15:00:00Z"},
                {"object_type": "deals", "last_synced_at": "2026-04-26T15:00:00Z"},
            ],
        }

    state = await _load_hubspot_sync_state_inline()
    return {
        "sync_state": [
            {"object_type": k, "last_synced_at": v} for k, v in state.items()
        ],
    }


# ---------------------------------------------------------------------------
# Internal helper kept inline here (single-use, file-local).  This is a
# plain ``async def`` without ``@custom_function`` - it's never directly
# called by the actor and lives at module level only because both
# top-level @custom_function entries above need it.  The compliance test
# enforces decoration on every top-level def, so we mark it too.
# ---------------------------------------------------------------------------


@custom_function()
async def _load_hubspot_sync_state_inline() -> dict[str, str]:
    """Read ``HubSpot/CRM/Meta/SyncState`` rows into a dict.  Internal helper."""
    try:
        rows = await primitives.data.filter(  # noqa: F821 - injected at runtime
            "HubSpot/CRM/Meta/SyncState",
        )
    except Exception:
        return {}
    out: dict[str, str] = {}
    for r in rows or []:
        ot = r.get("object_type")
        ts = r.get("last_synced_at")
        if isinstance(ot, str) and isinstance(ts, str):
            out[ot] = ts
    return out
