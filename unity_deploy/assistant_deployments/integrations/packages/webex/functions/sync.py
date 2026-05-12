"""Webex sync orchestrator + state.

``run_webex_sync_tick`` is the entrypoint the scenario runtime calls on
its interval.  It dispatches per-object sync functions, gates by
per-object cadence config, aggregates returned tables, and emits an
audit row.

Mirrors employment_hero/sync.py.
"""

from __future__ import annotations

from unity.function_manager.custom import custom_function


@custom_function()
async def run_webex_sync_tick(
    full: bool = False,
    mock: bool = True,
) -> dict:
    """Run a single Webex sync tick.

    Reads per-object watermarks from ``Webex/Meta/SyncState``,
    dispatches each enabled object type's ``sync_*`` function,
    aggregates returned tables into one envelope, and appends an audit
    row to ``Webex/Meta/SyncRuns``.

    Per-object cadence is gated by ``WEBEX_SYNC_OBJECT_INTERVALS`` so
    operators can tune freshness without changing this code.

    Parameters
    ----------
    full : bool
        Ignore watermarks and pull everything.
    mock : bool
        Return a tiny synthetic envelope without touching the API.
    """
    import datetime as _dt
    import importlib
    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._config import (
        get_webex_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._sync_helpers import (
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
        "people": ("_sync_people", "sync_webex_people"),
        "rooms": ("_sync_rooms", "sync_webex_rooms"),
        "meetings": ("_sync_meetings", "sync_webex_meetings"),
        "recordings": ("_sync_recordings", "sync_webex_recordings"),
        "transcripts": ("_sync_transcripts", "sync_webex_transcripts"),
    }

    cfg = get_webex_config()
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    schema_version = "webex.comms.sync.v1"

    if mock:
        from unity_deploy.assistant_deployments.integrations.packages.webex.functions._sync_meetings import (
            sync_webex_meetings,
        )
        from unity_deploy.assistant_deployments.integrations.packages.webex.functions._sync_recordings import (
            sync_webex_recordings,
        )

        m = await sync_webex_meetings(mock=True)
        r = await sync_webex_recordings(mock=True)
        finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        tables = {**m["tables"], **r["tables"]}
        tables["sync_state"] = [
            {
                "object_type": "meetings",
                "last_synced_at": finished,
                "last_status": "ok",
            },
            {
                "object_type": "recordings",
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
                    }
                ),
                "errors_json": "[]",
                "config_snapshot_json": str(
                    {
                        "sync_min_interval_seconds": cfg["sync_min_interval_seconds"],
                        "api_page_size": cfg["api_page_size"],
                        "mirror_transcripts": cfg["mirror_transcripts"],
                    }
                ),
                "mode": "mock",
            }
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
                }
            )
            return
        module_stem, fn_name = object_to_sync_fn[object_key]
        try:
            module = importlib.import_module(
                f"unity_deploy.assistant_deployments.integrations.packages."
                f"webex.functions.{module_stem}"
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
                }
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
                        "meeting_lookback_days",
                        "mirror_transcripts",
                    )
                }
            ),
            "mode": "live",
        }
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
async def get_webex_sync_state(mock: bool = True) -> dict:
    """Return current per-object watermarks plus the most recent run."""
    if mock:
        return {
            "sync_state": [
                {
                    "object_type": "meetings",
                    "last_synced_at": "2026-05-06T09:00:00Z",
                    "last_status": "ok",
                },
                {
                    "object_type": "recordings",
                    "last_synced_at": "2026-05-06T09:00:00Z",
                    "last_status": "ok",
                },
            ],
            "latest_run": {
                "started_at": "2026-05-06T09:00:00Z",
                "finished_at": "2026-05-06T09:00:18Z",
                "row_totals_json": "{'meetings': 8, 'recordings': 3}",
                "errors_json": "[]",
                "mode": "mock",
            },
        }

    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._sync_helpers import (
        load_latest_run,
        load_sync_state_rows,
    )

    state_rows = await load_sync_state_rows()
    latest = await load_latest_run()
    return {"sync_state": state_rows, "latest_run": latest}


@custom_function()
async def probe_webex_tier(force: bool = False, mock: bool = True) -> dict:
    """Discover which Webex capabilities the connected user's token covers.

    Caches results in ``Webex/Meta/Capabilities`` for
    ``WEBEX_TIER_PROBE_TTL_SECONDS`` (default 24h).  Pass ``force=True``
    to bypass the cache.
    """
    import datetime as _dt

    if mock:
        return {
            "probed_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(),
            "ttl_seconds": 86_400,
            "capabilities": {
                "people": {"available": True, "status_code": 200},
                "rooms": {"available": True, "status_code": 200},
                "meetings": {"available": True, "status_code": 200},
                "recordings": {"available": True, "status_code": 200},
                "transcripts": {
                    "available": False,
                    "status_code": 403,
                    "hint": "Webex Integration app lacks meeting:transcripts_read scope.",
                },
            },
        }

    from unity.manager_registry import ManagerRegistry
    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._config import (
        get_webex_config,
    )
    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._capabilities import (
        run_tier_probe,
    )
    from unity_deploy.assistant_deployments.integrations.packages.webex.functions._sync_helpers import (
        seconds_since,
    )

    cfg = get_webex_config()
    dm = ManagerRegistry.get_data_manager()

    if not force:
        try:
            cached = await dm.filter(
                "Webex/Meta/Capabilities",
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
            "Webex/Meta/Capabilities",
            rows=[{"probed_at": probed_at, "capabilities": capabilities}],
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
