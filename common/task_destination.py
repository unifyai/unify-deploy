"""Helpers for task destination labels shared by Communication services."""

from __future__ import annotations

from typing import Any, Final, Mapping

from common.int_list_codec import normalize_int_list

PERSONAL_TASK_DESTINATION: Final[str] = "personal"
SPACE_TASK_DESTINATION_PREFIX: Final[str] = "space:"


def task_destination_space_id(destination: str | None) -> int | None:
    """Return the shared-space id encoded in a task destination label."""

    if destination in (None, PERSONAL_TASK_DESTINATION):
        return None
    if not isinstance(destination, str) or not destination.startswith(
        SPACE_TASK_DESTINATION_PREFIX,
    ):
        return None
    try:
        return int(destination[len(SPACE_TASK_DESTINATION_PREFIX) :])
    except ValueError:
        return None


def assistant_has_task_destination(
    assistant_data: Mapping[str, Any],
    destination: str | None,
) -> bool:
    """Return whether assistant metadata authorizes a task destination."""

    if destination in (None, PERSONAL_TASK_DESTINATION):
        return True
    space_id = task_destination_space_id(destination)
    if space_id is None:
        return False
    return space_id in set(
        normalize_int_list(
            assistant_data.get("space_ids") or [],
            field_name="space_ids",
        ),
    )
