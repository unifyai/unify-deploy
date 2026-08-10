"""Timing for the meeting brief.

All imports are kept inside function bodies so FunctionManager can exec
each function in an isolated namespace.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
def brief_window(
    meeting_start_iso: str,
    lead_minutes: int = 30,
    now_iso: str | None = None,
    history_days: int = 120,
) -> dict:
    """When to send a meeting's brief, and how far back to read for it.

    Answers both halves of the timing question in one call: whether this
    meeting is inside its lead time yet, and the window of history a brief
    should search for the last substantive exchange with its attendees.

    Parameters
    ----------
    meeting_start_iso : str
        ISO-8601 timestamp the meeting starts.
    lead_minutes : int
        How long before the start the brief should arrive.
    now_iso : str | None
        ISO-8601 timestamp to measure against. Defaults to the current
        local time; pass a value for reproducible runs.
    history_days : int
        How far back to search for prior exchanges with the attendees.

    Returns
    -------
    dict
        ``send_at`` — the ISO-8601 instant the brief is due; ``due_now``
        when that instant has passed and the meeting has not started;
        ``too_late`` when the meeting is already under way, which is when
        a brief would arrive after the user is in the room; and
        ``history_since`` bounding the search for prior exchanges.
    """
    from datetime import datetime, timedelta

    start = datetime.fromisoformat(meeting_start_iso)
    now = datetime.fromisoformat(now_iso) if now_iso else datetime.now(tz=start.tzinfo)

    send_at = start - timedelta(minutes=max(0, lead_minutes))
    return {
        "send_at": send_at.isoformat(),
        "due_now": send_at <= now < start,
        "too_late": now >= start,
        "history_since": (now - timedelta(days=max(1, history_days))).isoformat(),
        "minutes_until_start": round((start - now).total_seconds() / 60.0, 1),
    }
