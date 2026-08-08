"""Working-hours arithmetic for the inbound response budget.

All imports are kept inside function bodies so FunctionManager can exec
each function in an isolated namespace.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
def response_budget(
    created_iso: str,
    budget_hours: float = 4.0,
    now_iso: str | None = None,
    day_start_hour: int = 9,
    day_end_hour: int = 18,
) -> dict:
    """How much of an inbound lead's response budget is left.

    The budget is consumed by working hours only, so a lead that arrives
    at 17:55 on a Friday still has almost all of it on Monday morning.
    Weekends never count, and neither does time outside the working day.

    Parameters
    ----------
    created_iso : str
        ISO-8601 timestamp the lead arrived at.
    budget_hours : float
        Working hours allowed before the lead counts as overdue.
    now_iso : str | None
        ISO-8601 timestamp to measure against. Defaults to the current
        local time; pass a value for reproducible runs.
    day_start_hour : int
        Hour the working day opens, local time.
    day_end_hour : int
        Hour the working day closes, local time.

    Returns
    -------
    dict
        ``elapsed_working_hours`` since the lead arrived,
        ``remaining_working_hours`` of budget (never negative),
        ``overdue`` as a bool, and ``deadline`` — the ISO-8601 instant
        the budget runs out, already advanced past closed hours.
    """
    from datetime import datetime, time, timedelta

    def _clamp_into_day(moment: datetime) -> datetime:
        """The next instant at or after *moment* that is inside a working day."""
        while True:
            if moment.weekday() >= 5:
                moment = datetime.combine(
                    moment.date() + timedelta(days=1),
                    time(day_start_hour, 0),
                    tzinfo=moment.tzinfo,
                )
                continue
            opens = moment.replace(
                hour=day_start_hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            closes = moment.replace(
                hour=day_end_hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            if moment < opens:
                return opens
            if moment >= closes:
                moment = datetime.combine(
                    moment.date() + timedelta(days=1),
                    time(day_start_hour, 0),
                    tzinfo=moment.tzinfo,
                )
                continue
            return moment

    def _working_hours_between(start: datetime, end: datetime) -> float:
        total = 0.0
        cursor = _clamp_into_day(start)
        while cursor < end:
            closes = cursor.replace(
                hour=day_end_hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            segment_end = min(closes, end)
            total += (segment_end - cursor).total_seconds() / 3600.0
            if segment_end >= end:
                break
            cursor = _clamp_into_day(closes)
        return round(total, 3)

    def _advance_working_hours(start: datetime, hours: float) -> datetime:
        cursor = _clamp_into_day(start)
        remaining = hours
        while remaining > 0:
            closes = cursor.replace(
                hour=day_end_hour,
                minute=0,
                second=0,
                microsecond=0,
            )
            available = (closes - cursor).total_seconds() / 3600.0
            if remaining <= available:
                return cursor + timedelta(hours=remaining)
            remaining -= available
            cursor = _clamp_into_day(closes)
        return cursor

    created = datetime.fromisoformat(created_iso)
    now = (
        datetime.fromisoformat(now_iso) if now_iso else datetime.now(tz=created.tzinfo)
    )

    elapsed = _working_hours_between(created, now)
    remaining = max(0.0, round(budget_hours - elapsed, 3))
    return {
        "elapsed_working_hours": elapsed,
        "remaining_working_hours": remaining,
        "overdue": elapsed >= budget_hours,
        "deadline": _advance_working_hours(created, budget_hours).isoformat(),
    }
