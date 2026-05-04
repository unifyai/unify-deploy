"""Resolved-at-runtime config for the HubSpot package.

All fields have safe defaults.  Override via env vars on the assistant
(set them through Settings -> Secrets in the console exactly like
``HUBSPOT_PRIVATE_APP_TOKEN``).  Optional ``HUBSPOT_CONFIG_JSON`` env
var can override anything in one blob.

Underscore-prefixed so FunctionManager skips this file - it's library
code, not a registered tool.
"""

from __future__ import annotations


def get_hubspot_config() -> dict:
    """Resolve the full HubSpot config dict.  Re-read on every call so
    env-var changes take effect on the next sync tick without a redeploy."""
    cfg = {
        # ----- Sync cadence -----------------------------------------------
        "sync_min_interval_seconds": _int("HUBSPOT_SYNC_MIN_INTERVAL_SECONDS", 300),
        "object_intervals": _kv_int(
            "HUBSPOT_SYNC_OBJECT_INTERVALS",
            default={"properties": 86400, "owners": 3600, "engagements": 1800},
        ),

        # ----- Object selection -------------------------------------------
        "sync_objects": _list(
            "HUBSPOT_SYNC_OBJECTS",
            default=[
                "contacts", "companies", "deals", "tickets",
                "line_items", "products", "quotes",
                "owners", "pipelines", "lists", "associations",
                "properties", "feedback", "goals", "custom_objects",
            ],
        ),
        "sync_engagements": _bool("HUBSPOT_SYNC_ENGAGEMENTS", default=False),
        "sync_engagement_types": _list(
            "HUBSPOT_SYNC_ENGAGEMENT_TYPES",
            default=["calls", "emails", "meetings", "notes", "tasks"],
        ),
        "sync_hubs": _list(
            "HUBSPOT_SYNC_HUBS",
            default=["crm", "engagements", "marketing", "sales", "service"],
        ),
        "sync_custom_objects": _bool("HUBSPOT_SYNC_CUSTOM_OBJECTS", default=True),
        "sync_email_events": _bool("HUBSPOT_SYNC_EMAIL_EVENTS", default=False),
        "sync_audit_logs": _bool("HUBSPOT_SYNC_AUDIT_LOGS", default=False),

        # ----- API behaviour ----------------------------------------------
        "api_page_size": _int("HUBSPOT_API_PAGE_SIZE", 100),
        "max_pages_per_sync": _int_or_none("HUBSPOT_MAX_PAGES_PER_SYNC", None),
        "request_timeout_seconds": _int("HUBSPOT_REQUEST_TIMEOUT_SECONDS", 30),
        "rate_limit_max_retries": _int("HUBSPOT_RATE_LIMIT_MAX_RETRIES", 3),
        "rate_limit_backoff_factor": _float("HUBSPOT_RATE_LIMIT_BACKOFF_FACTOR", 1.5),

        # ----- Property selection -----------------------------------------
        "contact_properties": _list_or_all("HUBSPOT_CONTACT_PROPERTIES", default="all"),
        "company_properties": _list_or_all("HUBSPOT_COMPANY_PROPERTIES", default="all"),
        "deal_properties": _list_or_all("HUBSPOT_DEAL_PROPERTIES", default="all"),
        "ticket_properties": _list_or_all("HUBSPOT_TICKET_PROPERTIES", default="all"),

        # ----- Embeddings -------------------------------------------------
        "embed_enabled": _bool("HUBSPOT_EMBED_ENABLED", default=True),
        "embed_strategy": _choice(
            "HUBSPOT_EMBED_STRATEGY",
            choices=("along", "after", "off"),
            default="along",
        ),

        # ----- Local-query freshness --------------------------------------
        "local_freshness_threshold_seconds": _int(
            "HUBSPOT_LOCAL_FRESHNESS_THRESHOLD_SECONDS", 3600,
        ),

        # ----- Mutation mirroring -----------------------------------------
        "mirror_mutations_to_datamanager": _bool(
            "HUBSPOT_MIRROR_MUTATIONS_TO_DATAMANAGER", default=True,
        ),

        # ----- High-stakes write gates ------------------------------------
        "allow_broadcast_email": _bool("HUBSPOT_ALLOW_BROADCAST_EMAIL", default=False),
        "allow_broadcast_sms": _bool("HUBSPOT_ALLOW_BROADCAST_SMS", default=False),
        "allow_cms_publish": _bool("HUBSPOT_ALLOW_CMS_PUBLISH", default=False),
        "allow_delete": _bool("HUBSPOT_ALLOW_DELETE", default=False),

        # ----- Capability probe cache TTL ---------------------------------
        "tier_probe_ttl_seconds": _int("HUBSPOT_TIER_PROBE_TTL_SECONDS", 86400),
    }

    raw = _env("HUBSPOT_CONFIG_JSON", "").strip()
    if raw:
        import json
        try:
            override = json.loads(raw)
            if isinstance(override, dict):
                cfg.update(override)
        except json.JSONDecodeError:
            pass
    return cfg


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    import os
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _int_or_none(name: str, default: int | None) -> int | None:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    if not raw:
        return list(default)
    return [s.strip() for s in raw.split(",") if s.strip()]


def _list_or_all(name: str, default):
    raw = _env(name)
    if not raw:
        return default
    if raw.strip().lower() == "all":
        return "all"
    return [s.strip() for s in raw.split(",") if s.strip()]


def _kv_int(name: str, default: dict[str, int]) -> dict[str, int]:
    """Parse ``key:int,key:int`` form."""
    raw = _env(name)
    if not raw:
        return dict(default)
    out: dict[str, int] = {}
    for piece in raw.split(","):
        if ":" not in piece:
            continue
        k, v = piece.split(":", 1)
        try:
            out[k.strip()] = int(v.strip())
        except ValueError:
            continue
    return out or dict(default)


def _choice(name: str, *, choices: tuple[str, ...], default: str) -> str:
    raw = _env(name).strip().lower()
    return raw if raw in choices else default
