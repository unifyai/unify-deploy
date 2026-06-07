"""Encoding helpers for shared-team summaries crossing text boundaries."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

TeamSummaryPayload = dict[str, int | str]


def normalize_team_summaries(
    value: Iterable[object],
    *,
    field_name: str,
) -> list[TeamSummaryPayload]:
    """Return a strict list of shared-team summary dictionaries."""

    if isinstance(value, (str, bytes)):
        raise ValueError(f"{field_name} must be a list of objects")
    try:
        summaries = list(value)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be a list of objects") from exc

    normalized: list[TeamSummaryPayload] = []
    for summary in summaries:
        if not isinstance(summary, dict):
            raise ValueError(f"{field_name} entries must be objects")
        normalized.append(_normalize_team_summary(summary, field_name=field_name))
    return normalized


def encode_team_summaries_for_form(
    value: Iterable[object],
    *,
    field_name: str,
) -> str:
    """Encode shared-team summaries for a form field."""

    return json.dumps(normalize_team_summaries(value, field_name=field_name))


def decode_team_summaries_from_form(
    value: str,
    *,
    field_name: str,
) -> list[TeamSummaryPayload]:
    """Decode shared-team summaries from a JSON-encoded form field."""

    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    return normalize_team_summaries(decoded, field_name=field_name)


def encode_team_summaries_for_env(
    value: Iterable[object],
    *,
    field_name: str,
) -> str:
    """Encode shared-team summaries for an environment variable."""

    summaries = normalize_team_summaries(value, field_name=field_name)
    return json.dumps(summaries) if summaries else ""


def _normalize_team_summary(
    summary: dict[Any, Any],
    *,
    field_name: str,
) -> TeamSummaryPayload:
    missing_keys = {"team_id", "name", "description"} - set(summary)
    if missing_keys:
        missing = ", ".join(sorted(missing_keys))
        raise ValueError(f"{field_name} entries are missing: {missing}")

    team_id = summary["team_id"]
    name = summary["name"]
    description = summary["description"]
    if not isinstance(team_id, int) or isinstance(team_id, bool):
        raise ValueError(f"{field_name}.team_id must be an integer")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{field_name}.name must be a non-empty string")
    if not isinstance(description, str) or not description:
        raise ValueError(f"{field_name}.description must be a non-empty string")

    return {
        "team_id": team_id,
        "name": name,
        "description": description,
    }
