"""Date arithmetic for contract renewal reminders.

All imports are kept inside function bodies so FunctionManager can exec
each function in an isolated namespace.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
def decision_dates(
    renewal_date: str,
    notice_days: int = 0,
    notice_months: int = 0,
    lead_days: int = 30,
    today: str | None = None,
) -> dict:
    """When a renewal must be decided, and when to warn about it.

    The decision date is the renewal date minus the contract's notice
    period; the warning date sits *lead_days* in front of it. A notice
    period stated in months is subtracted as calendar months, because
    "three months" and "90 days" are different spans and contracts mean
    the one they wrote.

    Parameters
    ----------
    renewal_date : str
        ISO date (``YYYY-MM-DD``) the current term ends.
    notice_days : int
        Notice period in days, when the contract states days.
    notice_months : int
        Notice period in calendar months, when the contract states
        months. Applied before ``notice_days``; both may be given.
    lead_days : int
        Desired warning ahead of the decision date.
    today : str | None
        ISO date to treat as today. Defaults to the current local date;
        pass a value for reproducible runs.

    Returns
    -------
    dict
        ``decision_date`` and ``warning_date`` as ISO dates,
        ``days_until_decision``, ``passed`` when the window has already
        closed, and ``lead_days_applied`` — shorter than requested when
        the contract itself leaves less room, so the reminder can say so.
    """
    from datetime import date, timedelta

    def _parse(value: str) -> date:
        return date.fromisoformat(value[:10])

    def _minus_months(anchor: date, months: int) -> date:
        if months <= 0:
            return anchor
        month_index = anchor.month - 1 - months
        year = anchor.year + month_index // 12
        month = month_index % 12 + 1
        # Clamp to the target month's length: 31 March minus one month is
        # 28 (or 29) February, never an invalid date.
        if month == 12:
            next_month_start = date(year + 1, 1, 1)
        else:
            next_month_start = date(year, month + 1, 1)
        last_day = (next_month_start - timedelta(days=1)).day
        return date(year, month, min(anchor.day, last_day))

    renewal = _parse(renewal_date)
    current = _parse(today) if today else date.today()

    decision = _minus_months(renewal, notice_months) - timedelta(days=notice_days)
    days_until_decision = (decision - current).days

    # Never warn in the past: a contract found late gets today's warning
    # and the shortened lead time reported, rather than a silent backdate.
    applied_lead = min(lead_days, max(0, days_until_decision))
    warning = decision - timedelta(days=applied_lead)

    return {
        "decision_date": decision.isoformat(),
        "warning_date": warning.isoformat(),
        "days_until_decision": days_until_decision,
        "passed": days_until_decision < 0,
        "lead_days_applied": applied_lead,
    }
