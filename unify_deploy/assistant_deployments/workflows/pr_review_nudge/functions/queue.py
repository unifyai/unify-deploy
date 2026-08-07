"""Working-day ageing for the pull request queue.

All imports are kept inside function bodies so FunctionManager can exec
each function in an isolated namespace.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
def working_days_since(
    last_activity_iso: str,
    now_iso: str | None = None,
    holidays: list[str] | None = None,
) -> dict:
    """How stale a pull request is, in working days.

    Counts whole working days that have fully elapsed since the last
    activity, so a review opened on Friday afternoon reads as one day
    old on Monday rather than three. Weekends and any dates passed in
    ``holidays`` are skipped entirely.

    Parameters
    ----------
    last_activity_iso : str
        ISO-8601 timestamp of the most recent review, comment or push.
    now_iso : str | None
        ISO-8601 timestamp to measure against. Defaults to the current
        local time; pass a value for reproducible runs.
    holidays : list[str] | None
        ISO dates (``YYYY-MM-DD``) that never count as working days.

    Returns
    -------
    dict
        ``working_days`` elapsed, ``calendar_days`` for contrast, and
        ``skipped_dates`` naming the weekend and holiday dates that were
        not counted — so a digest can explain an age that looks low.
    """
    from datetime import date, datetime, timedelta

    excluded = set(holidays or [])
    last = datetime.fromisoformat(last_activity_iso)
    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now(tz=last.tzinfo)

    if now <= last:
        return {"working_days": 0, "calendar_days": 0, "skipped_dates": []}

    working = 0
    skipped: list[str] = []
    cursor: date = last.date()
    while cursor < now.date():
        cursor = cursor + timedelta(days=1)
        iso = cursor.isoformat()
        if cursor.weekday() >= 5 or iso in excluded:
            skipped.append(iso)
            continue
        working += 1

    return {
        "working_days": working,
        "calendar_days": (now.date() - last.date()).days,
        "skipped_dates": skipped,
    }
