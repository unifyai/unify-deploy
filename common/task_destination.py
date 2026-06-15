"""Helpers for task destination labels shared by Communication services."""

from __future__ import annotations

from typing import Any, Final, Mapping

from common.int_list_codec import normalize_int_list

PERSONAL_TASK_DESTINATION: Final[str] = "personal"
TEAM_TASK_DESTINATION_PREFIX: Final[str] = "team:"


def task_destination_team_id(destination: str | None) -> int | None:
    """Return the shared-team id encoded in a task destination label."""

    if destination in (None, PERSONAL_TASK_DESTINATION):
        return None
    if not isinstance(destination, str) or not destination.startswith(
        TEAM_TASK_DESTINATION_PREFIX,
    ):
        return None
    try:
        return int(destination[len(TEAM_TASK_DESTINATION_PREFIX) :])
    except ValueError:
        return None


def assistant_has_task_destination(
    assistant_data: Mapping[str, Any],
    destination: str | None,
) -> bool:
    """Return whether assistant metadata authorizes a task destination."""

    if destination in (None, PERSONAL_TASK_DESTINATION):
        return True
    team_id = task_destination_team_id(destination)
    if team_id is None:
        return False
    return team_id in set(
        normalize_int_list(
            assistant_data.get("team_ids") or [],
            field_name="team_ids",
        ),
    )
