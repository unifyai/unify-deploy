"""Resolved-at-runtime config for the Salto KS package.

All fields have safe defaults.  Override via env vars on the assistant
(set them through Settings -> Secrets in the console exactly like
``SALTO_KS_CLIENT_ID`` — pasted by the customer in chat or via the
console).  Optional ``SALTO_KS_CONFIG_JSON`` env var can override
anything in one blob.

Underscore-prefixed so FunctionManager skips this file - it's library
code, not a registered tool.

Salto KS is **live-only** — there is no DataManager mirror or sync
orchestrator.  The cadence / write-gate / local-freshness keys below
are unused by the live tool today; they're kept harmless in case a
future iteration ships a mirror.  Auth + HTTP behaviour are the
sections this file actually drives at runtime.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Per-object cadence defaults (seconds).  Overridden by
# SALTO_KS_SYNC_OBJECT_INTERVALS.  Used by phase 1b's sync orchestrator;
# present here so phase 1b doesn't need to rewrite this file.
#
# Tuned to access-control use:
#   - access_events: 5 min (the marquee live table)
#   - lock_states: 15 min (battery / online drift)
#   - users / credentials / access_rights: hourly (HR / grant changes)
#   - access_groups / members: 6h (org-structural, slow)
#   - sites / locks / time_schedules: 24h (pure dimensions)
# ---------------------------------------------------------------------------

_DEFAULT_OBJECT_INTERVALS_SECONDS: dict[str, int] = {
    "access_events": 300,
    "lock_states": 900,
    "users": 3_600,
    "credentials": 3_600,
    "access_rights": 3_600,
    "access_groups": 21_600,
    "access_group_members": 21_600,
    "sites": 86_400,
    "locks": 86_400,
    "time_schedules": 86_400,
}

_ALL_SYNC_OBJECTS: list[str] = list(_DEFAULT_OBJECT_INTERVALS_SECONDS.keys())


def get_salto_ks_config() -> dict:
    """Resolve the full Salto KS config dict.

    Re-read on every call so env-var changes take effect on the next
    sync tick without a redeploy.
    """
    cfg = {
        # ----- Endpoint overrides ---------------------------------------
        # All three default to EU production hosts via ``_client.py``.
        # Set ``SALTO_KS_IDENTITY_HOST`` (and usually
        # ``SALTO_KS_BASE_URL``) for sandbox, non-EU regions, or any
        # BU-issued non-standard host.  ``SALTO_KS_OAUTH_TOKEN_URL``
        # is a full-URL escape hatch for the token endpoint only.
        "base_url_override": _env("SALTO_KS_BASE_URL", ""),
        "identity_host_override": _env("SALTO_KS_IDENTITY_HOST", ""),
        "oauth_token_url_override": _env("SALTO_KS_OAUTH_TOKEN_URL", ""),
        # ----- Auth ------------------------------------------------------
        # Scope string used when minting access tokens.  Salto's docs
        # use the coarse ``user_api.full_access`` scope for the Backend
        # Server flow; override only if the BU has issued a non-default
        # scope.
        "oauth_scopes": _env("SALTO_KS_OAUTH_SCOPES", ""),
        # Optional default site for list-call scoping.
        "default_site_id": _env("SALTO_KS_DEFAULT_SITE_ID", ""),
        # ----- Sync cadence (phase 1b) ----------------------------------
        "sync_min_interval_seconds": _int(
            "SALTO_KS_SYNC_MIN_INTERVAL_SECONDS",
            60,
        ),
        "object_intervals": _kv_int(
            "SALTO_KS_SYNC_OBJECT_INTERVALS",
            default=dict(_DEFAULT_OBJECT_INTERVALS_SECONDS),
        ),
        "sync_objects": _list(
            "SALTO_KS_SYNC_OBJECTS",
            default=list(_ALL_SYNC_OBJECTS),
        ),
        # ----- Access-event window --------------------------------------
        # How far back to pull access events on a delta tick when no
        # watermark is set.  Caps the initial-bootstrap lookback.
        "access_event_lookback_days": _int(
            "SALTO_KS_ACCESS_EVENT_LOOKBACK_DAYS",
            7,
        ),
        # ----- API behaviour --------------------------------------------
        "api_page_size": _int("SALTO_KS_API_PAGE_SIZE", 100),
        "max_pages_per_sync": _int_or_none(
            "SALTO_KS_MAX_PAGES_PER_SYNC",
            None,
        ),
        "request_timeout_seconds": _int(
            "SALTO_KS_REQUEST_TIMEOUT_SECONDS",
            30,
        ),
        "rate_limit_max_retries": _int(
            "SALTO_KS_RATE_LIMIT_MAX_RETRIES",
            3,
        ),
        "rate_limit_backoff_factor": _float(
            "SALTO_KS_RATE_LIMIT_BACKOFF_FACTOR",
            1.5,
        ),
        # ----- Capability probe cache TTL -------------------------------
        "tier_probe_ttl_seconds": _int(
            "SALTO_KS_TIER_PROBE_TTL_SECONDS",
            86_400,
        ),
        # ----- Write gates (phase 4) ------------------------------------
        # Off by default — phase 4 (writes) requires explicit opt-in
        # per deployment.  Read functions ignore these.
        "allow_user_delete": _bool("SALTO_KS_ALLOW_DELETE", default=False),
        "allow_access_revoke": _bool(
            "SALTO_KS_ALLOW_ACCESS_REVOKE",
            default=False,
        ),
        # ----- Local-query freshness (phase 1b) -------------------------
        # If unset, falls back to (object_interval * 2) inside
        # _local_helpers.py.
        "local_freshness_threshold_seconds": _int_or_none(
            "SALTO_KS_LOCAL_FRESHNESS_THRESHOLD_SECONDS",
            None,
        ),
    }

    raw = _env("SALTO_KS_CONFIG_JSON", "").strip()
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
