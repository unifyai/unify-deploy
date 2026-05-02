"""Tests for shared-space summary boundary encoding."""

from __future__ import annotations

import pytest

from common.space_summaries_codec import (
    decode_space_summaries_from_form,
    encode_space_summaries_for_env,
    encode_space_summaries_for_form,
    normalize_space_summaries,
)


def test_space_summaries_round_trip_form_and_env() -> None:
    """Shared-space summaries keep their object shape across text boundaries."""

    summaries = [
        {
            "space_id": 7,
            "name": "Support Ops",
            "description": "Customer support operations and escalation notes.",
        },
        {
            "space_id": 11,
            "name": "Marketing",
            "description": "Brand campaign planning and analytics workspace.",
        },
    ]

    form_value = encode_space_summaries_for_form(
        summaries,
        field_name="space_summaries",
    )

    assert (
        decode_space_summaries_from_form(
            form_value,
            field_name="space_summaries",
        )
        == summaries
    )
    assert encode_space_summaries_for_env([], field_name="space_summaries") == ""
    assert (
        encode_space_summaries_for_env(
            summaries,
            field_name="space_summaries",
        )
        == form_value
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"space_id": 7, "name": "Support", "description": "Missing list"},
        [{"name": "Support", "description": "Missing id"}],
        [{"space_id": True, "name": "Support", "description": "Boolean id"}],
        [{"space_id": 7, "name": "", "description": "Missing name"}],
        [{"space_id": 7, "name": "Support", "description": ""}],
    ],
)
def test_space_summaries_reject_malformed_payloads(payload) -> None:
    """Malformed summary payloads fail before reaching runtime startup."""

    with pytest.raises(ValueError):
        normalize_space_summaries(payload, field_name="space_summaries")
