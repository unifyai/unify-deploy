"""The window of releases one radar run should cover.

All imports are kept inside function bodies so FunctionManager can exec
each function in an isolated namespace.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
def release_window(
    last_run_iso: str | None = None,
    now_iso: str | None = None,
    default_days: int = 7,
) -> dict:
    """The period a run should read releases for.

    Anchored on the previous run rather than a fixed seven days, so a run
    started by hand covers the gap since the last one instead of
    re-reporting releases that were already read — and so a job that was
    held for a fortnight reports the fortnight rather than half of it.

    Parameters
    ----------
    last_run_iso : str | None
        ISO-8601 timestamp of the previous run. ``None`` on the first
        run, which falls back to *default_days*.
    now_iso : str | None
        ISO-8601 timestamp to measure to. Defaults to the current local
        time; pass a value for reproducible runs.
    default_days : int
        How far back a first run reaches.

    Returns
    -------
    dict
        ``since`` and ``until`` as ISO-8601 timestamps, the ``days`` they
        span, and ``is_catch_up`` — true when the window is wider than
        *default_days*, which is what a message should say out loud
        before listing an unusually long list.
    """
    from datetime import datetime, timedelta, timezone

    until = datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)
    if last_run_iso:
        since = datetime.fromisoformat(last_run_iso)
    else:
        since = until - timedelta(days=default_days)

    # A clock that went backwards, or a last run recorded in the future,
    # would otherwise produce a negative window that silently reads
    # nothing at all.
    if since >= until:
        since = until - timedelta(days=default_days)

    span = until - since
    days = span.total_seconds() / 86400
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "days": round(days, 2),
        "is_catch_up": days > default_days,
    }
