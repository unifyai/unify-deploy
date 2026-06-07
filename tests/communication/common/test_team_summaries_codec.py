"""Tests for shared-team summary boundary encoding."""

from __future__ import annotations

import pytest

from common.team_summaries_codec import (
    decode_team_summaries_from_form,
    encode_team_summaries_for_env,
    encode_team_summaries_for_form,
    normalize_team_summaries,
)


def test_team_summaries_round_trip_form_and_env() -> None:
    """Shared-team summaries keep their object shape across text boundaries."""

    summaries = [
        {
            "team_id": 7,
            "name": "Support Ops",
            "description": "Customer support operations and escalation notes.",
        },
        {
            "team_id": 11,
            "name": "Marketing",
            "description": "Brand campaign planning and analytics workspace.",
        },
    ]

    form_value = encode_team_summaries_for_form(
        summaries,
        field_name="team_summaries",
    )

    assert (
        decode_team_summaries_from_form(
            form_value,
            field_name="team_summaries",
        )
        == summaries
    )
    assert encode_team_summaries_for_env([], field_name="team_summaries") == ""
    assert (
        encode_team_summaries_for_env(
            summaries,
            field_name="team_summaries",
        )
        == form_value
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"team_id": 7, "name": "Support", "description": "Missing list"},
        [{"name": "Support", "description": "Missing id"}],
        [{"team_id": True, "name": "Support", "description": "Boolean id"}],
        [{"team_id": 7, "name": "", "description": "Missing name"}],
        [{"team_id": 7, "name": "Support", "description": ""}],
    ],
)
def test_team_summaries_reject_malformed_payloads(payload) -> None:
    """Malformed summary payloads fail before reaching runtime startup."""

    with pytest.raises(ValueError):
        normalize_team_summaries(payload, field_name="team_summaries")
