"""Resolved-at-runtime config for the Webex package.

All fields have safe defaults.  Override via env vars on the assistant
(set them through Settings -> Secrets in the console exactly like
``WEBEX_OAUTH_CLIENT_ID`` — pasted by the customer before Connect, or
``WEBEX_REFRESH_TOKEN`` written by the Console OAuth callback).
Optional ``WEBEX_CONFIG_JSON`` env
var can override anything in one blob.

Underscore-prefixed so FunctionManager skips this file - it's library
code, not a registered tool.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Per-object cadence defaults (seconds).  Overridden by
# WEBEX_SYNC_OBJECT_INTERVALS.
#
# Tuned to typical Webex use:
#   - Meetings + recordings drift through the day -> 15-30 min.
#   - Transcripts arrive 5-30 min post-meeting -> hourly catch-up.
#   - People + rooms drift slowly -> half-day to daily.
# ---------------------------------------------------------------------------

_DEFAULT_OBJECT_INTERVALS_SECONDS: dict[str, int] = {
    "meetings": 900,
    "recordings": 1_800,
    "transcripts": 3_600,
    "people": 86_400,
    "rooms": 21_600,
}

_ALL_SYNC_OBJECTS: list[str] = list(_DEFAULT_OBJECT_INTERVALS_SECONDS.keys())


def get_webex_config() -> dict:
    """Resolve the full Webex config dict.

    Re-read on every call so env-var changes take effect on the next
    sync tick without a redeploy.
    """
    cfg = {
        # ----- Auth + endpoint --------------------------------------------
        "base_url": _env("WEBEX_BASE_URL", "https://webexapis.com"),
        "oauth_token_url": _env(
            "WEBEX_OAUTH_TOKEN_URL",
            "https://webexapis.com/v1/access_token",
        ),
        # ----- Sync cadence -----------------------------------------------
        "sync_min_interval_seconds": _int(
            "WEBEX_SYNC_MIN_INTERVAL_SECONDS",
            300,
        ),
        "object_intervals": _kv_int(
            "WEBEX_SYNC_OBJECT_INTERVALS",
            default=dict(_DEFAULT_OBJECT_INTERVALS_SECONDS),
        ),
        # ----- Object selection -------------------------------------------
        "sync_objects": _list(
            "WEBEX_SYNC_OBJECTS",
            default=list(_ALL_SYNC_OBJECTS),
        ),
        # ----- Meeting window --------------------------------------------
        # How far back to pull meetings on a delta tick when no watermark
        # is set.  Webex's /meetings endpoint accepts ``from`` ISO time;
        # this caps the initial-bootstrap lookback.
        "meeting_lookback_days": _int("WEBEX_MEETING_LOOKBACK_DAYS", 90),
        # ----- API behaviour ----------------------------------------------
        "api_page_size": _int("WEBEX_API_PAGE_SIZE", 100),
        "max_pages_per_sync": _int_or_none(
            "WEBEX_MAX_PAGES_PER_SYNC",
            None,
        ),
        "request_timeout_seconds": _int(
            "WEBEX_REQUEST_TIMEOUT_SECONDS",
            30,
        ),
        "rate_limit_max_retries": _int(
            "WEBEX_RATE_LIMIT_MAX_RETRIES",
            3,
        ),
        "rate_limit_backoff_factor": _float(
            "WEBEX_RATE_LIMIT_BACKOFF_FACTOR",
            1.5,
        ),
        # ----- Transcript mirror gate -------------------------------------
        # Off by default — transcript bodies can be sensitive and
        # high-volume.  When off, transcript *metadata* is mirrored but
        # body text is fetched live on demand only.
        "mirror_transcripts": _bool(
            "WEBEX_MIRROR_TRANSCRIPTS",
            default=False,
        ),
        # ----- Capability probe cache TTL ---------------------------------
        "tier_probe_ttl_seconds": _int(
            "WEBEX_TIER_PROBE_TTL_SECONDS",
            86_400,
        ),
        # ----- Local-query freshness --------------------------------------
        # If unset, falls back to (object_interval * 2) inside
        # _local_helpers.py.
        "local_freshness_threshold_seconds": _int_or_none(
            "WEBEX_LOCAL_FRESHNESS_THRESHOLD_SECONDS",
            None,
        ),
    }

    raw = _env("WEBEX_CONFIG_JSON", "").strip()
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
    """Parse ``key:int,key:int`` form; values absent in the env merge
    with the supplied defaults."""
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
