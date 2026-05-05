"""Resolved-at-runtime config for the Employment Hero package.

All fields have safe defaults.  Override via env vars on the assistant
(set them through Settings -> Secrets in the console exactly like
``EMPLOYMENTHERO_OAUTH_CLIENT_ID``).  Optional ``EMPLOYMENTHERO_CONFIG_JSON``
env var can override anything in one blob.

Underscore-prefixed so FunctionManager skips this file - it's library
code, not a registered tool.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Per-object cadence defaults (seconds).  Overridden by
# EMPLOYMENTHERO_SYNC_OBJECT_INTERVALS.
#
# Tuned to ClientZeta's likely use:
#   - Workforce graph drifts slowly -> daily.
#   - Leave / timesheets / expenses are workday-active -> hourly-ish.
#   - Pay / qualifications / performance -> daily.
#   - Recognition is event-driven -> hourly.
# ---------------------------------------------------------------------------

_DEFAULT_OBJECT_INTERVALS_SECONDS: dict[str, int] = {
    "workforce": 86_400,
    "employee_personal": 86_400,
    "employee_notes": 21_600,
    "leave": 3_600,
    "timesheets": 1_800,
    "expenses": 3_600,
    "policies": 86_400,
    "documents": 43_200,
    "custom_fields": 86_400,
    "onboarding": 21_600,
    "qualifications": 43_200,
    "performance": 86_400,
    "recognition": 3_600,
    "surveys": 86_400,
    "learning": 21_600,
    "recruitment": 3_600,
    "pay": 86_400,
}

_ALL_SYNC_OBJECTS: list[str] = list(_DEFAULT_OBJECT_INTERVALS_SECONDS.keys())


def get_employmenthero_config() -> dict:
    """Resolve the full Employment Hero config dict.

    Re-read on every call so env-var changes take effect on the next
    sync tick without a redeploy.
    """
    cfg = {
        # ----- Auth + endpoint --------------------------------------------
        "base_url": _env(
            "EMPLOYMENTHERO_BASE_URL",
            "https://api.employmenthero.com",
        ),
        "organisation_id": _env("EMPLOYMENTHERO_ORGANISATION_ID", "") or None,
        # ----- Sync cadence -----------------------------------------------
        "sync_min_interval_seconds": _int(
            "EMPLOYMENTHERO_SYNC_MIN_INTERVAL_SECONDS",
            300,
        ),
        "object_intervals": _kv_int(
            "EMPLOYMENTHERO_SYNC_OBJECT_INTERVALS",
            default=dict(_DEFAULT_OBJECT_INTERVALS_SECONDS),
        ),
        # ----- Object selection -------------------------------------------
        "sync_objects": _list(
            "EMPLOYMENTHERO_SYNC_OBJECTS",
            default=list(_ALL_SYNC_OBJECTS),
        ),
        # ----- API behaviour ----------------------------------------------
        "api_page_size": _int("EMPLOYMENTHERO_API_PAGE_SIZE", 100),
        "max_pages_per_sync": _int_or_none(
            "EMPLOYMENTHERO_MAX_PAGES_PER_SYNC",
            None,
        ),
        "request_timeout_seconds": _int(
            "EMPLOYMENTHERO_REQUEST_TIMEOUT_SECONDS",
            30,
        ),
        "rate_limit_max_retries": _int(
            "EMPLOYMENTHERO_RATE_LIMIT_MAX_RETRIES",
            3,
        ),
        "rate_limit_backoff_factor": _float(
            "EMPLOYMENTHERO_RATE_LIMIT_BACKOFF_FACTOR",
            1.5,
        ),
        # ----- Sensitivity / redaction ------------------------------------
        # Critical at the snapshot boundary so DataManager analytics never
        # holds the raw values even if the live API would.
        "redact_performance_free_text": _bool(
            "EMPLOYMENTHERO_REVIEWS_REDACT_FREE_TEXT",
            default=True,
        ),
        "redact_employee_personal": _bool(
            "EMPLOYMENTHERO_EMPLOYEE_PERSONAL_REDACT",
            default=True,
        ),
        "pay_rate_bands": _bool(
            "EMPLOYMENTHERO_PAY_RATE_BANDS",
            default=True,
        ),
        # ----- Recruitment hyperparams ------------------------------------
        "recruitment_retention_days": _int(
            "EMPLOYMENTHERO_RECRUITMENT_RETENTION_DAYS",
            180,
        ),
        "recruitment_redact_pii": _bool(
            "EMPLOYMENTHERO_RECRUITMENT_REDACT_PII",
            default=True,
        ),
        "recruitment_include_rejected": _bool(
            "EMPLOYMENTHERO_RECRUITMENT_INCLUDE_REJECTED",
            default=False,
        ),
        # ----- Surveys ----------------------------------------------------
        # Always defer to EH's per-survey is_anonymous flag.  This env var
        # only forces an extra-conservative "treat all as anonymous" pass.
        "surveys_force_anonymous": _bool(
            "EMPLOYMENTHERO_SURVEYS_FORCE_ANONYMOUS",
            default=False,
        ),
        # ----- Mutation mirroring -----------------------------------------
        "mirror_mutations_to_datamanager": _bool(
            "EMPLOYMENTHERO_MIRROR_MUTATIONS_TO_DATAMANAGER",
            default=True,
        ),
        # ----- High-stakes write master gate ------------------------------
        # Required to be true for every submit_*/acknowledge_*/give_*/create_*
        # function to actually mutate live data.  Without it those functions
        # return a structured refusal envelope.
        "allow_high_stakes_writes": _bool(
            "EMPLOYMENTHERO_ALLOW_HIGH_STAKES_WRITES",
            default=False,
        ),
        # ----- Capability probe cache TTL ---------------------------------
        "tier_probe_ttl_seconds": _int(
            "EMPLOYMENTHERO_TIER_PROBE_TTL_SECONDS",
            86_400,
        ),
        # ----- Local-query freshness --------------------------------------
        # If unset, falls back to (object_interval * 2) inside local.py.
        "local_freshness_threshold_seconds": _int_or_none(
            "EMPLOYMENTHERO_LOCAL_FRESHNESS_THRESHOLD_SECONDS",
            None,
        ),
        # ----- Embeddings -------------------------------------------------
        "embed_enabled": _bool("EMPLOYMENTHERO_EMBED_ENABLED", default=False),
        "embed_strategy": _choice(
            "EMPLOYMENTHERO_EMBED_STRATEGY",
            choices=("along", "after", "off"),
            default="off",
        ),
    }

    raw = _env("EMPLOYMENTHERO_CONFIG_JSON", "").strip()
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


def _choice(name: str, *, choices: tuple[str, ...], default: str) -> str:
    raw = _env(name).strip().lower()
    return raw if raw in choices else default
