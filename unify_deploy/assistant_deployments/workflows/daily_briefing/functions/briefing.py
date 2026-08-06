"""Time helpers for the daily briefing.

All imports are kept inside function bodies so FunctionManager can exec
each function in an isolated namespace.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
def briefing_window(now_iso: str | None = None) -> dict:
    """The exact time window a morning briefing should scan.

    Calendar coverage runs from now to the end of the working day;
    inbox and slippage coverage reach back through the previous two
    working days, so a Monday briefing still sees Thursday's and
    Friday's traffic and weekends are never counted as waiting time.

    Parameters
    ----------
    now_iso : str | None
        ISO-8601 timestamp to anchor the window at. Defaults to the
        current local time; pass a value for reproducible runs.

    Returns
    -------
    dict
        ``calendar_start`` / ``calendar_end`` bounding the rest of the
        working day, ``inbox_since`` reaching back two working days,
        and ``working_days_back`` naming the dates that were counted.
    """
    from datetime import datetime, time, timedelta

    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now()

    end_of_day = datetime.combine(now.date(), time(18, 0), tzinfo=now.tzinfo)
    if now > end_of_day:
        end_of_day = now

    counted: list[str] = []
    cursor = now.date()
    while len(counted) < 2:
        cursor = cursor - timedelta(days=1)
        if cursor.weekday() < 5:
            counted.append(cursor.isoformat())
    inbox_since = datetime.combine(
        datetime.fromisoformat(counted[-1]).date(),
        time(0, 0),
        tzinfo=now.tzinfo,
    )

    return {
        "calendar_start": now.isoformat(),
        "calendar_end": end_of_day.isoformat(),
        "inbox_since": inbox_since.isoformat(),
        "working_days_back": counted,
    }
