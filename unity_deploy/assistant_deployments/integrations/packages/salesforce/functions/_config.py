"""Resolved-at-runtime config for the Salesforce package.

All fields have safe defaults.  Override via env vars on the assistant
(set them through Settings -> Secrets in the console exactly like
``SALESFORCE_CLIENT_ID`` — pasted by the customer before Connect, or
``SALESFORCE_REFRESH_TOKEN`` and ``SALESFORCE_INSTANCE_URL`` written by
the Console OAuth callback).  Optional ``SALESFORCE_CONFIG_JSON`` env
var can override anything in one blob.

Underscore-prefixed so FunctionManager skips this file - it's library
code, not a registered tool.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# OAuth host — fixed to production.  Sandbox (test.salesforce.com) and
# customer My Domain hosts are not supported in v0.  All token
# exchange / refresh calls land here; per-org REST traffic uses
# SALESFORCE_INSTANCE_URL written by the Console OAuth callback.
# ---------------------------------------------------------------------------

_LOGIN_URL = "https://login.salesforce.com"


# ---------------------------------------------------------------------------
# Per-object cadence defaults (seconds).  Overridden by
# SALESFORCE_SYNC_OBJECT_INTERVALS.
#
# Tuned to typical CRM use:
#   - Accounts + contacts drift slowly -> 30 min.
#   - Leads / opportunities / cases drift through the day -> 15 min.
# ---------------------------------------------------------------------------

_DEFAULT_OBJECT_INTERVALS_SECONDS: dict[str, int] = {
    "accounts": 1_800,
    "contacts": 1_800,
    "leads": 900,
    "opportunities": 900,
    "cases": 900,
}

_ALL_SYNC_OBJECTS: list[str] = list(_DEFAULT_OBJECT_INTERVALS_SECONDS.keys())


def get_salesforce_config() -> dict:
    """Resolve the full Salesforce config dict.

    Re-read on every call so env-var changes take effect on the next
    sync tick without a redeploy.
    """
    cfg = {
        # ----- Auth + endpoint --------------------------------------------
        # Login URL is fixed at module level; instance URL is per-org
        # and written by the Console OAuth callback.
        "login_url": _LOGIN_URL,
        "oauth_token_url": f"{_LOGIN_URL}/services/oauth2/token",
        "oauth_authorize_url": f"{_LOGIN_URL}/services/oauth2/authorize",
        "api_version": _env("SALESFORCE_API_VERSION", "v60.0"),
        # ----- Sync cadence -----------------------------------------------
        "sync_min_interval_seconds": _int(
            "SALESFORCE_SYNC_MIN_INTERVAL_SECONDS",
            300,
        ),
        "object_intervals": _kv_int(
            "SALESFORCE_SYNC_OBJECT_INTERVALS",
            default=dict(_DEFAULT_OBJECT_INTERVALS_SECONDS),
        ),
        # ----- Object selection -------------------------------------------
        "sync_objects": _list(
            "SALESFORCE_SYNC_OBJECTS",
            default=list(_ALL_SYNC_OBJECTS),
        ),
        # ----- API behaviour ----------------------------------------------
        "api_page_size": _int("SALESFORCE_API_PAGE_SIZE", 200),
        "max_pages_per_sync": _int_or_none(
            "SALESFORCE_MAX_PAGES_PER_SYNC",
            None,
        ),
        "request_timeout_seconds": _int(
            "SALESFORCE_REQUEST_TIMEOUT_SECONDS",
            30,
        ),
        "rate_limit_max_retries": _int(
            "SALESFORCE_RATE_LIMIT_MAX_RETRIES",
            3,
        ),
        "rate_limit_backoff_factor": _float(
            "SALESFORCE_RATE_LIMIT_BACKOFF_FACTOR",
            1.5,
        ),
        # ----- Local-query freshness --------------------------------------
        # If unset, falls back to (object_interval * 2) inside callers.
        "local_freshness_threshold_seconds": _int_or_none(
            "SALESFORCE_LOCAL_FRESHNESS_THRESHOLD_SECONDS",
            None,
        ),
    }

    raw = _env("SALESFORCE_CONFIG_JSON", "").strip()
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
