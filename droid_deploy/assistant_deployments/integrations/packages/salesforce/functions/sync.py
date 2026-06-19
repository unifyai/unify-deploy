"""Salesforce sync orchestrator + state.

``run_salesforce_sync_tick`` is the entrypoint the scenario runtime
calls on its interval.  It dispatches per-object sync functions, gates
by per-object cadence config, aggregates returned tables, and emits an
audit row.

Mirrors webex/sync.py.
"""

from __future__ import annotations

from droid.function_manager.custom import custom_function


@custom_function()
async def run_salesforce_sync_tick(
    full: bool = False,
    mock: bool = True,
) -> dict:
    """Run a single Salesforce sync tick.

    Reads per-object watermarks from ``Salesforce/Meta/SyncState``,
    dispatches each enabled object type's ``sync_*`` function,
    aggregates returned tables into one envelope, and appends an audit
    row to ``Salesforce/Meta/SyncRuns``.

    Per-object cadence is gated by ``SALESFORCE_SYNC_OBJECT_INTERVALS``
    so operators can tune freshness without changing this code.

    Parameters
    ----------
    full : bool
        Ignore watermarks and pull everything.
    mock : bool
        Return a tiny synthetic envelope without touching the API.
    """
    import datetime as _dt
    import importlib
    from droid_deploy.assistant_deployments.integrations.packages.salesforce.functions._config import (
        get_salesforce_config,
    )
    from droid_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_helpers import (
        get_state_watermark,
        load_sync_state,
        seconds_since,
    )

    # Object key -> (module stem, function name).  Inlined per
    # FunctionManager isolation rule — no module-level globals.
    # Underscore-prefixed module stems point at internal sync helpers
    # (FunctionManager skips them at discovery; this orchestrator
    # imports them via importlib).
    object_to_sync_fn: dict[str, tuple[str, str]] = {
        "accounts": ("_sync_accounts", "sync_salesforce_accounts"),
        "contacts": ("_sync_contacts", "sync_salesforce_contacts"),
        "leads": ("_sync_leads", "sync_salesforce_leads"),
        "opportunities": ("_sync_opportunities", "sync_salesforce_opportunities"),
        "cases": ("_sync_cases", "sync_salesforce_cases"),
    }

    cfg = get_salesforce_config()
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    schema_version = "salesforce.crm.sync.v1"

    if mock:
        from droid_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_accounts import (
            sync_salesforce_accounts,
        )
        from droid_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_opportunities import (
            sync_salesforce_opportunities,
        )

        a = await sync_salesforce_accounts(mock=True)
        o = await sync_salesforce_opportunities(mock=True)
        finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        tables = {**a["tables"], **o["tables"]}
        tables["sync_state"] = [
            {
                "object_type": "accounts",
                "last_synced_at": finished,
                "last_status": "ok",
            },
            {
                "object_type": "opportunities",
                "last_synced_at": finished,
                "last_status": "ok",
            },
        ]
        tables["sync_runs"] = [
            {
                "started_at": started,
                "finished_at": finished,
                "row_totals_json": str(
                    {
                        k: len(v)
                        for k, v in tables.items()
                        if k not in ("sync_state", "sync_runs")
                    },
                ),
                "errors_json": "[]",
                "config_snapshot_json": str(
                    {
                        "sync_min_interval_seconds": cfg["sync_min_interval_seconds"],
                        "api_page_size": cfg["api_page_size"],
                        "api_version": cfg["api_version"],
                    },
                ),
                "mode": "mock",
            },
        ]
        return {
            "schema_version": schema_version,
            "tables": tables,
            "metadata": {
                "started_at": started,
                "finished_at": finished,
                "mode": "mock",
            },
        }

    sync_state = await load_sync_state()
    errors: list[dict] = []
    skipped: list[dict] = []
    row_totals: dict[str, int] = {}
    aggregated: dict[str, list] = {}

    async def _dispatch(object_key: str) -> None:
        if object_key not in cfg["sync_objects"]:
            return  # operator disabled this object via env
        if object_key not in object_to_sync_fn:
            return
        gap = seconds_since(get_state_watermark(sync_state, object_key))
        min_gap = cfg["object_intervals"].get(
            object_key,
            cfg["sync_min_interval_seconds"],
        )
        if not full and gap is not None and gap < min_gap:
            skipped.append(
                {
                    "object_type": object_key,
                    "reason": "cadence_not_due",
                    "gap_seconds": gap,
                    "min_gap_seconds": min_gap,
                },
            )
            return
        module_stem, fn_name = object_to_sync_fn[object_key]
        try:
            module = importlib.import_module(
                f"droid_deploy.assistant_deployments.integrations.packages."
                f"salesforce.functions.{module_stem}",
            )
        except ImportError as e:
            errors.append({"object_type": object_key, "error": f"import: {e}"})
            return
        fn = getattr(module, fn_name, None)
        if fn is None:
            errors.append(
                {
                    "object_type": object_key,
                    "error": f"missing function {fn_name} in module {module_stem}",
                },
            )
            return
        kwargs: dict = {"mock": False}
        watermark = get_state_watermark(sync_state, object_key)
        if not full and watermark:
            kwargs["since"] = watermark
        try:
            envelope = await fn(**kwargs)
        except Exception as e:  # noqa: BLE001
            errors.append({"object_type": object_key, "error": f"raised: {e!r}"})
            return
        if isinstance(envelope, dict) and envelope.get("status_code") == 403:
            skipped.append({"object_type": object_key, "reason": "permission_403"})
            return
        if not isinstance(envelope, dict):
            errors.append({"object_type": object_key, "error": "non-dict return"})
            return
        if "error" in envelope and not envelope.get("tables"):
            errors.append({"object_type": object_key, "error": envelope["error"]})
            return
        for table_name, rows in (envelope.get("tables") or {}).items():
            if not rows:
                continue
            aggregated.setdefault(table_name, []).extend(rows)
            row_totals[table_name] = row_totals.get(table_name, 0) + len(rows)

    for k in cfg["sync_objects"]:
        await _dispatch(k)

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    aggregated["sync_state"] = [
        {"object_type": k, "last_synced_at": finished, "last_status": "ok"}
        for k in cfg["sync_objects"]
        if k not in {entry["object_type"] for entry in errors}
    ]
    aggregated["sync_runs"] = [
        {
            "started_at": started,
            "finished_at": finished,
            "row_totals_json": str(row_totals),
            "errors_json": str(errors),
            "skipped_json": str(skipped),
            "config_snapshot_json": str(
                {
                    k: cfg[k]
                    for k in (
                        "sync_min_interval_seconds",
                        "api_page_size",
                        "api_version",
                    )
                },
            ),
            "mode": "live",
        },
    ]

    return {
        "schema_version": schema_version,
        "tables": aggregated,
        "metadata": {
            "started_at": started,
            "finished_at": finished,
            "row_totals": row_totals,
            "errors": errors,
            "skipped": skipped,
        },
    }


@custom_function()
async def get_salesforce_sync_state(mock: bool = True) -> dict:
    """Return current per-object watermarks plus the most recent run."""
    if mock:
        return {
            "sync_state": [
                {
                    "object_type": "accounts",
                    "last_synced_at": "2026-05-06T09:00:00Z",
                    "last_status": "ok",
                },
                {
                    "object_type": "opportunities",
                    "last_synced_at": "2026-05-06T09:00:00Z",
                    "last_status": "ok",
                },
            ],
            "latest_run": {
                "started_at": "2026-05-06T09:00:00Z",
                "finished_at": "2026-05-06T09:00:24Z",
                "row_totals_json": "{'salesforce_accounts': 12, 'salesforce_opportunities': 5}",
                "errors_json": "[]",
                "mode": "mock",
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.salesforce.functions._sync_helpers import (
        load_latest_run,
        load_sync_state_rows,
    )

    state_rows = await load_sync_state_rows()
    latest = await load_latest_run()
    return {"sync_state": state_rows, "latest_run": latest}
