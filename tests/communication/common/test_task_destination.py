"""Tests for shared-team task destination validation."""

from __future__ import annotations

from common.task_destination import (
    assistant_has_task_destination,
    task_destination_team_id,
)


def test_task_destination_team_id_parses_team_labels() -> None:
    assert task_destination_team_id("personal") is None
    assert task_destination_team_id(None) is None
    assert task_destination_team_id("team:7") == 7
    assert task_destination_team_id("org:7") is None
    assert task_destination_team_id("team:not-int") is None


def test_assistant_has_task_destination_checks_team_memberships() -> None:
    assistant_data = {"team_ids": [3, 7]}

    assert assistant_has_task_destination(assistant_data, "personal") is True
    assert assistant_has_task_destination(assistant_data, "team:7") is True
    assert assistant_has_task_destination(assistant_data, "team:9") is False
    assert assistant_has_task_destination(assistant_data, "org:7") is False
