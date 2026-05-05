"""Employment Hero sync orchestrator + state.

``run_employmenthero_sync_tick`` is the entrypoint the scenario runtime
calls on its interval.  It dispatches per-object sync functions, gates
by per-object cadence config, aggregates returned tables, and emits an
audit row.

Mirrors hubspot/sync.py.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


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
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )

    # Object key -> (module stem, function name).  Inlined per
    # FunctionManager isolation rule — no module-level globals.
    object_to_sync_fn: dict[str, tuple[str, str]] = {
        "workforce":          ("workforce",         "sync_workforce"),
        "employee_personal":  ("employee_personal", "sync_employee_personal"),
        "employee_notes":     ("employee_notes",    "sync_employee_notes"),
        "leave":              ("leave",             "sync_leave"),
        "timesheets":         ("timesheets",        "sync_timesheets"),
        "expenses":           ("expenses",          "sync_expenses"),
        "policies":           ("policies",          "sync_policies"),
        "documents":          ("documents",         "sync_documents"),
        "custom_fields":      ("custom_fields",     "sync_custom_fields"),
        "onboarding":         ("onboarding",        "sync_onboarding"),
        "qualifications":     ("qualifications",    "sync_qualifications"),
        "performance":        ("performance",       "sync_performance"),
        "recognition":        ("recognition",       "sync_recognition"),
        "surveys":            ("surveys",           "sync_surveys"),
        "learning":           ("learning",          "sync_learning"),
        "recruitment":        ("recruitment",       "sync_recruitment"),
        "pay":                ("pay",               "sync_pay"),
    }

    cfg = get_employmenthero_config()
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    schema_version = "employment-hero.workforce.sync.v1"

    if mock:
        # Minimal aggregated mock envelope - exercises the contract.
        from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions.workforce import (
            sync_workforce,
        )
        from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions.qualifications import (
            sync_qualifications,
        )

        wf = await sync_workforce(mock=True)
        qu = await sync_qualifications(mock=True)
        finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        tables = {**wf["tables"], **qu["tables"]}
        tables["sync_state"] = [
            {"object_type": "workforce",      "last_synced_at": finished, "last_status": "ok"},
            {"object_type": "qualifications", "last_synced_at": finished, "last_status": "ok"},
        ]
        tables["sync_runs"] = [{
            "started_at": started,
            "finished_at": finished,
            "row_totals_json": str({k: len(v) for k, v in tables.items() if k not in ("sync_state", "sync_runs")}),
            "errors_json": "[]",
            "config_snapshot_json": str({
                "sync_min_interval_seconds": cfg["sync_min_interval_seconds"],
                "api_page_size": cfg["api_page_size"],
            }),
            "mode": "mock",
        }]
        return {
            "schema_version": schema_version,
            "tables": tables,
            "metadata": {
                "started_at": started, "finished_at": finished, "mode": "mock",
            },
        }

    sync_state = await _load_sync_state()
    errors: list[dict] = []
    skipped: list[dict] = []
    row_totals: dict[str, int] = {}
    aggregated: dict[str, list] = {}

    async def _dispatch(object_key: str) -> None:
        if object_key not in cfg["sync_objects"]:
            return  # operator disabled this object via env
        if object_key not in object_to_sync_fn:
            return
        gap = _seconds_since(_get_state_watermark(sync_state, object_key))
        min_gap = cfg["object_intervals"].get(
            object_key, cfg["sync_min_interval_seconds"],
        )
        if not full and gap is not None and gap < min_gap:
            skipped.append({"object_type": object_key, "reason": "cadence_not_due", "gap_seconds": gap, "min_gap_seconds": min_gap})
            return
        module_stem, fn_name = object_to_sync_fn[object_key]
        try:
            module = importlib.import_module(
                f"unity_deploy.assistant_deployments.integrations.packages."
                f"employment_hero.functions.{module_stem}"
            )
        except ImportError as e:
            errors.append({"object_type": object_key, "error": f"import: {e}"})
            return
        fn = getattr(module, fn_name, None)
        if fn is None:
            errors.append({
                "object_type": object_key,
                "error": f"missing function {fn_name} in module {module_stem}",
            })
            return
        kwargs: dict = {"mock": False}
        watermark = _get_state_watermark(sync_state, object_key)
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
    aggregated["sync_runs"] = [{
        "started_at": started,
        "finished_at": finished,
        "row_totals_json": str(row_totals),
        "errors_json": str(errors),
        "skipped_json": str(skipped),
        "config_snapshot_json": str({
            k: cfg[k] for k in (
                "sync_min_interval_seconds",
                "api_page_size",
                "recruitment_retention_days",
                "recruitment_redact_pii",
                "redact_performance_free_text",
                "pay_rate_bands",
                "redact_employee_personal",
            )
        }),
        "mode": "live",
    }]

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
async def get_sync_state(mock: bool = True) -> dict:
    """Return current per-object watermarks plus the most recent run."""
    if mock:
        return {
            "sync_state": [
                {"object_type": "workforce", "last_synced_at": "2026-04-30T02:00:00Z", "last_status": "ok"},
                {"object_type": "leave", "last_synced_at": "2026-04-30T09:00:00Z", "last_status": "ok"},
            ],
            "latest_run": {
                "started_at": "2026-04-30T09:00:00Z",
                "finished_at": "2026-04-30T09:00:42Z",
                "row_totals_json": "{'employees': 12, 'leave_requests': 4}",
                "errors_json": "[]",
                "mode": "mock",
            },
        }

    state_rows = await _load_sync_state_rows()
    latest = await _load_latest_run()
    return {"sync_state": state_rows, "latest_run": latest}


@custom_function()
async def probe_tier(force: bool = False, mock: bool = True) -> dict:
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
                "workforce":      {"available": True, "status_code": 200},
                "leave":          {"available": True, "status_code": 200},
                "qualifications": {"available": True, "status_code": 200},
                "pay":            {"available": False, "status_code": 403,
                                   "hint": "Token lacks pay scope."},
            },
        }

    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._config import (
        get_employmenthero_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.employment_hero.functions._capabilities import (
        run_tier_probe,
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
                age = _seconds_since(probed)
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
            rows=[{
                "probed_at": probed_at,
                "capabilities": capabilities,
            }],
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


# ---------------------------------------------------------------------------
# Helpers (decorated so FunctionManager registers them; depended on by the
# entrypoints above)
# ---------------------------------------------------------------------------


@custom_function()
async def _load_sync_state() -> dict:
    """Read the per-object watermark map from DataManager.  Returns an
    empty dict on the first run before any state row has been written.
    """
    from unity.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        rows = await dm.filter(
            "EmploymentHero/Workforce/Meta/SyncState",
            limit=200,
        )
    except Exception:
        return {}
    return {r["object_type"]: r for r in (rows or []) if r.get("object_type")}


@custom_function()
async def _load_sync_state_rows() -> list:
    """Same as ``_load_sync_state`` but returns the raw row list (stable
    shape for the public ``get_sync_state``)."""
    from unity.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        return await dm.filter(
            "EmploymentHero/Workforce/Meta/SyncState",
            limit=200,
        )
    except Exception:
        return []


@custom_function()
async def _load_latest_run() -> dict | None:
    """Most recent sync_runs row, or None."""
    from unity.manager_registry import ManagerRegistry

    dm = ManagerRegistry.get_data_manager()
    try:
        rows = await dm.filter(
            "EmploymentHero/Workforce/Meta/SyncRuns",
            limit=1,
            order_by="started_at desc",
        )
    except Exception:
        return None
    return (rows or [None])[0]


@custom_function()
def _get_state_watermark(state: dict, object_key: str) -> str | None:
    """Pull the ``last_synced_at`` timestamp for an object type."""
    row = state.get(object_key) if state else None
    if not row:
        return None
    return row.get("last_synced_at")


@custom_function()
def _seconds_since(iso_ts: str | None) -> int | None:
    """Return seconds since an ISO-8601 timestamp; None if no input."""
    if not iso_ts:
        return None
    import datetime as _dt
    try:
        parsed = _dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    delta = _dt.datetime.now(tz=_dt.timezone.utc) - parsed
    return int(delta.total_seconds())
