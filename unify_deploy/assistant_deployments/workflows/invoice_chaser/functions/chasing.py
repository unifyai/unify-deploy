"""Stage arithmetic for the overdue invoice ladder.

All imports are kept inside function bodies so FunctionManager can exec
each function in an isolated namespace.
"""

from __future__ import annotations

from unify.function_manager.custom import custom_function


@custom_function()
def chase_stage(
    due_iso: str,
    now_iso: str | None = None,
    chases_sent: int = 0,
    max_chases: int = 3,
    last_chase_iso: str | None = None,
) -> dict:
    """Which chase an overdue invoice is due, if any.

    Encodes the escalation ladder: a reminder in the first week, a
    follow-up to day 21, a firm note after that. Also answers the two
    questions that stop a ladder becoming harassment — whether the
    invoice has exhausted its chases, and whether one was already sent
    inside the last seven days.

    Parameters
    ----------
    due_iso : str
        ISO-8601 date or timestamp the invoice fell due.
    now_iso : str | None
        ISO-8601 timestamp to measure against. Defaults to the current
        local time; pass a value for reproducible runs.
    chases_sent : int
        How many chases this invoice has already received.
    max_chases : int
        The ceiling after which the ladder stops and a person takes over.
    last_chase_iso : str | None
        ISO-8601 timestamp of the most recent chase, when there is one.

    Returns
    -------
    dict
        ``days_overdue``, ``stage`` (``"not_due"``, ``"reminder"``,
        ``"follow_up"`` or ``"firm"``), ``should_draft`` and, when it is
        false, ``hold_reason`` naming which rule held it back.
    """
    from datetime import datetime

    def _parse(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed

    due = _parse(due_iso)
    now = _parse(now_iso) if now_iso else datetime.now(tz=due.tzinfo)

    days_overdue = (now.date() - due.date()).days
    if days_overdue <= 0:
        return {
            "days_overdue": days_overdue,
            "stage": "not_due",
            "should_draft": False,
            "hold_reason": "not yet due",
        }

    if days_overdue <= 7:
        stage = "reminder"
    elif days_overdue <= 21:
        stage = "follow_up"
    else:
        stage = "firm"

    if chases_sent >= max_chases:
        return {
            "days_overdue": days_overdue,
            "stage": stage,
            "should_draft": False,
            "hold_reason": f"already chased {chases_sent} times — hand to a person",
        }

    if last_chase_iso:
        since_last = (now.date() - _parse(last_chase_iso).date()).days
        if since_last < 7:
            return {
                "days_overdue": days_overdue,
                "stage": stage,
                "should_draft": False,
                "hold_reason": f"chased {since_last} days ago — one chase per week",
            }

    return {"days_overdue": days_overdue, "stage": stage, "should_draft": True}
