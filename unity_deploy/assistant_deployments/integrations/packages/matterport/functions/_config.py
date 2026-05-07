"""Resolved-at-runtime config for the Matterport package.

All fields have safe defaults.  Override via env vars on the assistant.
``MATTERPORT_CONFIG_JSON`` can override anything in one blob.

Underscore-prefixed so FunctionManager skips this file at discovery.
"""

from __future__ import annotations

# Per-object cadence defaults (seconds).  Overridden by
# MATTERPORT_SYNC_OBJECT_INTERVALS.
#
# Matterport models drift slowly (new scans + occasional re-edits) -> daily.
# View stats need to be hourly to be useful for sales hand-offs.

_DEFAULT_OBJECT_INTERVALS_SECONDS: dict[str, int] = {
    "models": 86_400,
    "view_stats": 3_600,
    "view_events": 3_600,
}

_ALL_SYNC_OBJECTS: list[str] = ["models", "view_stats"]


def get_matterport_config() -> dict:
    """Resolve the full Matterport config dict.

    Re-read on every call so env-var changes take effect on the next
    sync tick without a redeploy.
    """
    cfg = {
        # ----- Auth + endpoint --------------------------------------------
        "base_url": _env("MATTERPORT_BASE_URL", "https://api.matterport.com"),
        "org_id": _env("MATTERPORT_ORG_ID", "") or None,
        # ----- Sync cadence -----------------------------------------------
        "sync_min_interval_seconds": _int(
            "MATTERPORT_SYNC_MIN_INTERVAL_SECONDS",
            300,
        ),
        "object_intervals": _kv_int(
            "MATTERPORT_SYNC_OBJECT_INTERVALS",
            default=dict(_DEFAULT_OBJECT_INTERVALS_SECONDS),
        ),
        # ----- Object selection -------------------------------------------
        "sync_objects": _list(
            "MATTERPORT_SYNC_OBJECTS",
            default=list(_ALL_SYNC_OBJECTS),
        ),
        # Granular per-session events are opt-in (high volume).
        "sync_view_events": _bool(
            "MATTERPORT_SYNC_VIEW_EVENTS",
            default=False,
        ),
        # ----- View-stats window ------------------------------------------
        "view_stats_lookback_days": _int(
            "MATTERPORT_VIEW_STATS_LOOKBACK_DAYS",
            7,
        ),
        # ----- API behaviour ----------------------------------------------
        "api_page_size": _int("MATTERPORT_API_PAGE_SIZE", 25),
        "max_pages_per_sync": _int_or_none(
            "MATTERPORT_MAX_PAGES_PER_SYNC",
            None,
        ),
        "request_timeout_seconds": _int(
            "MATTERPORT_REQUEST_TIMEOUT_SECONDS",
            30,
        ),
        "rate_limit_max_retries": _int(
            "MATTERPORT_RATE_LIMIT_MAX_RETRIES",
            3,
        ),
        "rate_limit_backoff_factor": _float(
            "MATTERPORT_RATE_LIMIT_BACKOFF_FACTOR",
            1.5,
        ),
        # ----- Capability probe cache TTL ---------------------------------
        "tier_probe_ttl_seconds": _int(
            "MATTERPORT_TIER_PROBE_TTL_SECONDS",
            86_400,
        ),
        # ----- Local-query freshness --------------------------------------
        # If unset, falls back to (object_interval * 2) inside _local_helpers.
        "local_freshness_threshold_seconds": _int_or_none(
            "MATTERPORT_LOCAL_FRESHNESS_THRESHOLD_SECONDS",
            None,
        ),
        # ----- Internal-label convention ---------------------------------
        # Matterport models can have an internal_label set by the property
        # manager; if it matches this regex, sync auto-creates a model<->unit
        # link with confidence 1.0.  See matterport_unit_linking guidance.
        "internal_label_unit_regex": _env(
            "MATTERPORT_INTERNAL_LABEL_UNIT_REGEX",
            r"^unit:(?P<unit_id>[A-Za-z0-9-_]+)$",
        ),
    }

    raw = _env("MATTERPORT_CONFIG_JSON", "").strip()
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


def _kv_int(name: str, default: dict[str, int]) -> dict[str, int]:
    raw = _env(name)
    if not raw:
        return dict(default)
    out: dict[str, int] = dict(default)
    for piece in raw.split(","):
        if ":" not in piece:
            continue
        k, v = piece.split(":", 1)
        try:
            out[k.strip()] = int(v.strip())
        except ValueError:
            continue
    return out
