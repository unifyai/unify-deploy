"""The stretch of a working day one ship log covers.

All imports are kept inside function bodies so FunctionManager can exec
each function in an isolated namespace.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
def working_day_window(
    now_iso: str | None = None,
    day_starts_at: str = "05:00",
) -> dict:
    """The window of work one log should read.

    Runs from the start of the current working day to now, so a log
    written by hand at noon covers the morning rather than a fixed span
    ending in the future. Work done late the previous night belongs to
    the day it felt like, which is why the day boundary is configurable
    rather than midnight.

    Parameters
    ----------
    now_iso : str | None
        ISO-8601 timestamp to measure from. Defaults to the current
        local time; pass a value for reproducible runs.
    day_starts_at : str
        24-hour ``HH:MM`` where one working day ends and the next
        begins. Anything after this time counts as today.

    Returns
    -------
    dict
        ``since`` and ``until`` as ISO-8601 timestamps, the ``hours``
        they span, ``is_weekend`` for the day being read, and
        ``spans_midnight`` — true when the window reaches back into the
        previous calendar date, which a log should not present as
        yesterday's work.
    """
    from datetime import datetime, timedelta, timezone

    until = datetime.fromisoformat(now_iso) if now_iso else datetime.now(timezone.utc)
    hour, _, minute = day_starts_at.partition(":")
    boundary = until.replace(
        hour=int(hour),
        minute=int(minute or 0),
        second=0,
        microsecond=0,
    )
    # Before the boundary, the working day that is still running started
    # yesterday — 01:00 on Tuesday is Monday's work.
    since = boundary if until >= boundary else boundary - timedelta(days=1)

    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "hours": round((until - since).total_seconds() / 3600, 2),
        "is_weekend": since.weekday() >= 5,
        "spans_midnight": since.date() != until.date(),
    }
