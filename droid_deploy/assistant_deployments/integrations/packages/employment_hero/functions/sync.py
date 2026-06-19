"""Employment Hero sync orchestrator + state.

``run_employmenthero_sync_tick`` is the entrypoint the scenario runtime
calls on its interval.  It dispatches per-object sync functions, gates
by per-object cadence config, aggregates returned tables, and emits an
audit row.

Mirrors hubspot/sync.py.
"""

from __future__ import annotations

from droid.function_manager.custom import custom_function


@custom_function()
async def run_employmenthero_sync_tick(
    full: bool = False,
    mock: bool = True,
) -> dict:
    """Run a single Employment Hero sync tick.

    Reads per-object watermarks from
    ``EmploymentHero/Workforce/Meta/SyncState``, dispatches each enabled
    object type's ``sync_*`` function, aggregates returned tables into
    one envelope, and appends an audit row to SyncRuns.

    Per-object cadence is gated by ``EMPLOYMENTHERO_SYNC_OBJECT_INTERVALS``
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
    from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )
    from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._sync_helpers import (
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
        "workforce": ("_sync_workforce", "sync_employmenthero_workforce"),
        "employee_personal": (
            "_sync_employee_personal",
            "sync_employmenthero_employee_personal",
        ),
        "employee_notes": (
            "_sync_employee_notes",
            "sync_employmenthero_employee_notes",
        ),
        "leave": ("_sync_leave", "sync_employmenthero_leave"),
        "timesheets": ("_sync_timesheets", "sync_employmenthero_timesheets"),
        "expenses": ("_sync_expenses", "sync_employmenthero_expenses"),
        "policies": ("_sync_policies", "sync_employmenthero_policies"),
        "documents": ("_sync_documents", "sync_employmenthero_documents"),
        "custom_fields": ("_sync_custom_fields", "sync_employmenthero_custom_fields"),
        "onboarding": ("_sync_onboarding", "sync_employmenthero_onboarding"),
        "qualifications": (
            "_sync_qualifications",
            "sync_employmenthero_qualifications",
        ),
        "performance": ("_sync_performance", "sync_employmenthero_performance"),
        "recognition": ("_sync_recognition", "sync_employmenthero_recognition"),
        "surveys": ("_sync_surveys", "sync_employmenthero_surveys"),
        "learning": ("_sync_learning", "sync_employmenthero_learning"),
        "recruitment": ("_sync_recruitment", "sync_employmenthero_recruitment"),
        "pay": ("_sync_pay", "sync_employmenthero_pay"),
    }

    cfg = get_employmenthero_config()
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    schema_version = "employment-hero.workforce.sync.v1"

    if mock:
        # Minimal aggregated mock envelope - exercises the contract.
        from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._sync_workforce import (
            sync_employmenthero_workforce,
        )
        from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._sync_qualifications import (
            sync_employmenthero_qualifications,
        )

        wf = await sync_employmenthero_workforce(mock=True)
        qu = await sync_employmenthero_qualifications(mock=True)
        finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        tables = {**wf["tables"], **qu["tables"]}
        tables["sync_state"] = [
            {
                "object_type": "workforce",
                "last_synced_at": finished,
                "last_status": "ok",
            },
            {
                "object_type": "qualifications",
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
                f"employment_hero.functions.{module_stem}",
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
            skipped.append({"object_type": object_key, "reason": "tier_gated_403"})
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
                        "recruitment_retention_days",
                        "recruitment_redact_pii",
                        "redact_performance_free_text",
                        "pay_rate_bands",
                        "redact_employee_personal",
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
async def get_employmenthero_sync_state(mock: bool = True) -> dict:
    """Return current per-object watermarks plus the most recent run."""
    if mock:
        return {
            "sync_state": [
                {
                    "object_type": "workforce",
                    "last_synced_at": "2026-04-30T02:00:00Z",
                    "last_status": "ok",
                },
                {
                    "object_type": "leave",
                    "last_synced_at": "2026-04-30T09:00:00Z",
                    "last_status": "ok",
                },
            ],
            "latest_run": {
                "started_at": "2026-04-30T09:00:00Z",
                "finished_at": "2026-04-30T09:00:42Z",
                "row_totals_json": "{'employees': 12, 'leave_requests': 4}",
                "errors_json": "[]",
                "mode": "mock",
            },
        }

    from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._sync_helpers import (
        load_latest_run,
        load_sync_state_rows,
    )

    state_rows = await load_sync_state_rows()
    latest = await load_latest_run()
    return {"sync_state": state_rows, "latest_run": latest}


@custom_function()
async def probe_employmenthero_tier(force: bool = False, mock: bool = True) -> dict:
    """Discover which Employment Hero capabilities the active token covers.

    Caches results in ``EmploymentHero/Workforce/Meta/Capabilities`` for
    ``EMPLOYMENTHERO_TIER_PROBE_TTL_SECONDS`` (default 24h).  Pass
    ``force=True`` to bypass the cache.
    """
    import datetime as _dt

    if mock:
        return {
            "probed_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
            "ttl_seconds": 86_400,
            "capabilities": {
                "workforce": {"available": True, "status_code": 200},
                "leave": {"available": True, "status_code": 200},
                "qualifications": {"available": True, "status_code": 200},
                "pay": {
                    "available": False,
                    "status_code": 403,
                    "hint": "Token lacks pay scope.",
                },
            },
        }

    from droid.manager_registry import ManagerRegistry
    from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )
    from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._capabilities import (
        run_tier_probe,
    )
    from droid_deploy.assistant_deployments.integrations.packages.employment_hero.functions._sync_helpers import (
        seconds_since,
    )

    cfg = get_employmenthero_config()
    dm = ManagerRegistry.get_data_manager()

    if not force:
        try:
            cached = await dm.filter(
                "EmploymentHero/Workforce/Meta/Capabilities",
                limit=1,
                order_by="probed_at desc",
            )
        except Exception:
            cached = []
        if cached:
            row = cached[0]
            probed = row.get("probed_at")
            if probed:
                age = seconds_since(probed)
                if age is not None and age < cfg["tier_probe_ttl_seconds"]:
                    return {
                        "probed_at": probed,
                        "ttl_seconds": cfg["tier_probe_ttl_seconds"],
                        "capabilities": row.get("capabilities") or {},
                        "_from_cache": True,
                    }

    capabilities = await run_tier_probe()
    probed_at = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    try:
        dm.ingest(
            "EmploymentHero/Workforce/Meta/Capabilities",
            rows=[
                {
                    "probed_at": probed_at,
                    "capabilities": capabilities,
                },
            ],
            description="Most recent token capability probe.",
            unique_keys={"probed_at": "str"},
            infer_untyped_fields=True,
        )
    except Exception:
        pass
    return {
        "probed_at": probed_at,
        "ttl_seconds": cfg["tier_probe_ttl_seconds"],
        "capabilities": capabilities,
        "_from_cache": False,
    }
