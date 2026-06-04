"""Per-process usage metering and daily caps for the Valos package.

Underscore-prefixed so :func:`unity.function_manager.custom_functions.collect_custom_functions`
skips this file.

The package wraps two paid upstream services (OS Data Hub Premium and
PropertyData), so a runaway loop in any deployment can produce a real
bill regardless of whose subscription is in play (Unify-owned shared
account on the deploy-time path, or per-customer keys on the
Console-paste path).  The two upstream clients import
:func:`record_and_check` and call it before issuing each HTTP request;
when the per-day counter for a provider trips its ceiling,
:func:`quota_envelope` is returned as the response body and the actual
HTTP call is skipped.

Caps are deliberately permissive at the package level — the intent is
to stop a runaway loop, not to police normal usage.  Tighter per-
deployment policy can be layered on top once the runtime exposes the
deployment id; until then, both ceilings are process-wide.

Counters reset at the next call after midnight UTC.  Cross-process
aggregation (e.g. when more than one client deployment opts in) is a
follow-on; until then, both ceilings are process-wide.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Provider keys and defaults
# ---------------------------------------------------------------------------

PROVIDER_OS_TILES = "os_tiles"
PROVIDER_OS_NAMES_PLACES = "os_names_places"
PROVIDER_PROPERTYDATA = "propertydata"
PROVIDER_NOMINATIM = "nominatim"
PROVIDER_CQC = "cqc"

_DEFAULT_DAILY_LIMITS: dict[str, int] = {
    PROVIDER_OS_TILES: 5000,
    PROVIDER_OS_NAMES_PLACES: 1000,
    PROVIDER_PROPERTYDATA: 500,
    # Nominatim is unmetered upstream but caps at ~1 req/s; the daily
    # ceiling here protects against runaway-loop bursts and signals
    # politeness to the OSM operations team.
    PROVIDER_NOMINATIM: 2000,
    # CQC Syndication is unbilled but allows 2000 req/min with a
    # partnerCode.  A catchment resolution fans out across authority
    # listings + per-location detail calls, so the daily ceiling is
    # generous; it exists only to cap a runaway loop.
    PROVIDER_CQC: 10000,
}


# ---------------------------------------------------------------------------
# Counter store
# ---------------------------------------------------------------------------


class _DailyCounters:
    """Thread-safe per-day call counter, keyed by provider."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._date: str | None = None
        self._counts: dict[str, int] = {}

    def increment(self, provider: str) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            if self._date != today:
                self._date = today
                self._counts = {}
            self._counts[provider] = self._counts.get(provider, 0) + 1
            return self._counts[provider]

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


_COUNTERS = _DailyCounters()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_and_check(provider: str) -> tuple[bool, int, int]:
    """Record a call against ``provider`` and return ``(exceeded, count, limit)``.

    The caller increments the counter unconditionally and inspects the
    return value: if ``exceeded`` is True, the upstream HTTP call should
    be skipped and :func:`quota_envelope` returned to the actor.
    """
    count = _COUNTERS.increment(provider)
    limit = _resolve_limit(provider)
    return count > limit, count, limit


def quota_envelope(provider: str, count: int, limit: int) -> dict:
    """Structured failure envelope for quota-exceeded conditions."""
    return {
        "error": (
            f"Daily quota for {provider} exceeded ({count} > {limit}).  "
            "The Valos package halts upstream calls when its per-day cap "
            "is reached to protect the upstream subscription from a "
            "runaway loop."
        ),
        "status_code": None,
        "provider": provider,
        "calls_today": count,
        "daily_limit": limit,
        "hint": (
            f"Override the cap by setting VALOS_{provider.upper()}_DAILY_LIMIT "
            "in the deployment env, or wait until midnight UTC for the "
            "counter to reset."
        ),
    }


def usage_snapshot() -> dict[str, int]:
    """Return the current day's call counts per provider — for telemetry/debug."""
    return _COUNTERS.snapshot()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_limit(provider: str) -> int:
    env_key = f"VALOS_{provider.upper()}_DAILY_LIMIT"
    raw = os.environ.get(env_key)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return _DEFAULT_DAILY_LIMITS.get(provider, 1000)
