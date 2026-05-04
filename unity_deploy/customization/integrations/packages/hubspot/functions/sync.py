"""HubSpot sync orchestrator + state.

``run_hubspot_sync_tick`` is the entrypoint the scenario runtime calls
on its interval.  It dispatches per-object sync functions, gates by
per-object cadence config, and emits an audit row."""

from __future__ import annotations

from unity.function_manager.custom import custom_function


_OBJECT_TO_SYNC_FN: dict[str, tuple[str, str]] = {
    # object key -> (function module, function name)
    "contacts":          ("contacts",            "sync_contacts"),
    "companies":         ("companies",           "sync_companies"),
    "deals":             ("deals",               "sync_deals"),
    "tickets":           ("tickets",             "sync_tickets"),
    "line_items":        ("line_items",          "sync_line_items"),
    "products":          ("products",            "sync_products"),
    "quotes":            ("quotes",              "sync_quotes"),
    "owners":            ("owners",              "sync_owners"),
    "pipelines":         ("pipelines",           "sync_pipelines"),
    "lists":             ("lists",               "sync_lists"),
    "associations":      ("associations",        "sync_associations"),
    "properties":        ("properties",          "sync_properties"),
    "feedback":          ("feedback",            "sync_feedback"),
    "goals":             ("goals",               "sync_goals"),
    "custom_objects":    ("custom_objects",      "sync_custom_objects"),
}

_ENGAGEMENT_TO_SYNC_FN: dict[str, tuple[str, str]] = {
    "calls":    ("engagement_calls",    "sync_calls"),
    "emails":   ("engagement_emails",   "sync_emails"),
    "meetings": ("engagement_meetings", "sync_meetings"),
    "notes":    ("engagement_notes",    "sync_notes"),
    "tasks":    ("engagement_tasks",    "sync_tasks"),
}

_MARKETING_TO_SYNC_FN: dict[str, tuple[str, str]] = {
    "forms":         ("marketing_forms",         "sync_marketing_forms"),
    "campaigns":     ("marketing_campaigns",     "sync_campaigns"),
    "emails":        ("marketing_emails",        "sync_marketing_emails"),
    "workflows":     ("marketing_workflows",     "sync_marketing_workflows"),
    "ctas":          ("marketing_ctas",          "sync_ctas"),
    "subscriptions": ("marketing_subscriptions", "sync_subscriptions"),
    "events":        ("marketing_events",        "sync_marketing_events"),
}

_SALES_TO_SYNC_FN: dict[str, tuple[str, str]] = {
    "sequences":      ("sales_sequences",      "sync_sequences"),
    "templates":      ("sales_templates",      "sync_sales_templates"),
    "snippets":       ("sales_snippets",       "sync_sales_snippets"),
    "documents":      ("sales_documents",      "sync_sales_documents"),
    "meeting_links":  ("sales_meeting_links",  "sync_meeting_links"),
}

_SERVICE_TO_SYNC_FN: dict[str, tuple[str, str]] = {
    "conversations": ("service_conversations",   "sync_conversations"),
    "kb_articles":   ("service_knowledge_base",  "sync_kb_articles"),
    "chatflows":     ("service_chatflows",       "sync_chatflows"),
}


@custom_function()
async def run_hubspot_sync_tick(full: bool = False, mock: bool = True) -> dict:
    """Run a single sync tick.

    Reads the per-object watermark from ``HubSpot/CRM/Meta/SyncState``,
    dispatches each enabled sync function, aggregates the returned tables
    into one envelope, and appends an audit row to ``SyncRuns``.

    The scenario runtime ingests every table in the returned envelope per
    its ``data_targets`` map.

    Parameters
    ----------
    full : bool
        Ignore the watermark and pull everything.
    mock : bool
        Return a tiny synthetic envelope without touching the API.
    """
    import datetime as _dt
    from unity_deploy.customization.integrations.packages.hubspot.functions._config import (
        get_hubspot_config,
    )

    cfg = get_hubspot_config()
    started = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    if mock:
        # Minimal aggregated mock envelope - exercises the contract.
        from unity_deploy.customization.integrations.packages.hubspot.functions.contacts import sync_contacts
        from unity_deploy.customization.integrations.packages.hubspot.functions.companies import sync_companies
        from unity_deploy.customization.integrations.packages.hubspot.functions.deals import sync_deals

        contacts_env = await sync_contacts(mock=True)
        companies_env = await sync_companies(mock=True)
        deals_env = await sync_deals(mock=True)

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
            "sync_runs": [{
                "started_at": started, "finished_at": finished,
                "row_totals_json": str({"contacts": len(contacts_env["tables"]["contacts"]),
                                         "companies": len(companies_env["tables"]["companies"]),
                                         "deals": len(deals_env["tables"]["deals"])}),
                "errors_json": "[]",
                "config_snapshot_json": str({k: cfg[k] for k in
                                              ("sync_min_interval_seconds", "api_page_size")}),
                "mode": "mock",
            }],
        }
        return {
            "schema_version": "hubspot.crm.sync.v1",
            "tables": tables,
            "metadata": {"started_at": started, "finished_at": finished, "mode": "mock"},
        }

    sync_state = await _load_sync_state()
    errors: list[dict] = []
    row_totals: dict[str, int] = {}
    aggregated_tables: dict[str, list] = {}

    async def _dispatch(object_key: str,
                        registry: dict[str, tuple[str, str]],
                        *,
                        accepts_since: bool = True) -> None:
        if object_key not in registry:
            return
        gap = _seconds_since(sync_state.get(object_key))
        min_gap = cfg["object_intervals"].get(object_key, cfg["sync_min_interval_seconds"])
        if not full and gap is not None and gap < min_gap:
            return
        module_stem, fn_name = registry[object_key]
        try:
            module = __import__(
                f"unity_deploy.customization.integrations.packages.hubspot.functions.{module_stem}",
                fromlist=[fn_name],
            )
        except ImportError as e:
            errors.append({"object_type": object_key, "error": f"import failed: {e}"})
            return
        fn = getattr(module, fn_name, None)
        if fn is None:
            errors.append({"object_type": object_key, "error": f"function {fn_name} missing"})
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
            await _dispatch(k, _OBJECT_TO_SYNC_FN, accepts_since=k in (
                "contacts", "companies", "deals", "tickets",
                "line_items", "products", "quotes",
            ))
        if cfg["sync_custom_objects"]:
            await _dispatch("custom_objects", _OBJECT_TO_SYNC_FN, accepts_since=False)

    # Engagements
    if "engagements" in cfg["sync_hubs"] and cfg["sync_engagements"]:
        for k in cfg["sync_engagement_types"]:
            await _dispatch(k, _ENGAGEMENT_TO_SYNC_FN, accepts_since=True)

    # Marketing / Sales / Service - sync all known surfaces by default; tier
    # gating handles 403s gracefully.
    if "marketing" in cfg["sync_hubs"]:
        for k in _MARKETING_TO_SYNC_FN:
            await _dispatch(k, _MARKETING_TO_SYNC_FN, accepts_since=False)
    if "sales" in cfg["sync_hubs"]:
        for k in _SALES_TO_SYNC_FN:
            await _dispatch(k, _SALES_TO_SYNC_FN, accepts_since=False)
    if "service" in cfg["sync_hubs"]:
        for k in _SERVICE_TO_SYNC_FN:
            await _dispatch(k, _SERVICE_TO_SYNC_FN, accepts_since=False)

    finished = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    aggregated_tables["sync_state"] = [
        {"object_type": k, "last_synced_at": finished}
        for k in row_totals
    ]
    aggregated_tables["sync_runs"] = [{
        "started_at": started, "finished_at": finished,
        "row_totals_json": str(row_totals),
        "errors_json": str(errors),
        "config_snapshot_json": str({k: cfg[k] for k in
                                      ("sync_min_interval_seconds", "api_page_size",
                                       "sync_hubs", "sync_objects")}),
        "mode": "real",
    }]
    return {
        "schema_version": "hubspot.crm.sync.v1",
        "tables": aggregated_tables,
        "metadata": {
            "started_at": started, "finished_at": finished,
            "row_totals": row_totals, "errors": errors, "mode": "real",
        },
    }


@custom_function()
async def get_sync_state(mock: bool = True) -> dict:
    """Return the current sync watermarks per object type from DataManager."""
    if mock:
        return {
            "sync_state": [
                {"object_type": "contacts", "last_synced_at": "2026-04-26T15:00:00Z"},
                {"object_type": "companies", "last_synced_at": "2026-04-26T15:00:00Z"},
                {"object_type": "deals", "last_synced_at": "2026-04-26T15:00:00Z"},
            ],
        }

    state = await _load_sync_state()
    return {"sync_state": [{"object_type": k, "last_synced_at": v} for k, v in state.items()]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_sync_state() -> dict[str, str]:
    """Read ``HubSpot/CRM/Meta/SyncState`` rows into a ``{object_type: last_synced_at}`` dict."""
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


def _seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    import datetime as _dt
    try:
        ts = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (_dt.datetime.now(tz=_dt.timezone.utc) - ts).total_seconds()
