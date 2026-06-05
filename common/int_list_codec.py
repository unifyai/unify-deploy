"""Encoding helpers for integer-list values crossing text boundaries.

Communication carries list-shaped identifiers through JSON payloads, form
fields, and environment variables. These helpers keep those representations
strict so callers do not silently collapse malformed membership data into an
empty list.
"""

from __future__ import annotations

import json
from typing import Iterable


def normalize_int_list(value: Iterable[int], *, field_name: str) -> list[int]:
    """Return ``value`` as a list of integers or raise ``ValueError``.

    ``bool`` is rejected even though it subclasses ``int`` because these fields
    represent identifiers, not flags.
    """

    try:
        values = list(value)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be a list of integers") from exc

    if any(not isinstance(item, int) or isinstance(item, bool) for item in values):
        raise ValueError(f"{field_name} must be a list of integers")
    return values


def encode_int_list_for_form(value: Iterable[int], *, field_name: str) -> str:
    """Encode an integer list for a form field."""

    return json.dumps(normalize_int_list(value, field_name=field_name))


def decode_int_list_from_form(value: str, *, field_name: str) -> list[int]:
    """Decode a JSON-encoded integer list from a form field."""

    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    return normalize_int_list(decoded, field_name=field_name)


def encode_int_list_for_env(value: Iterable[int], *, field_name: str) -> str:
    """Encode an integer list for an environment variable."""

    return ",".join(
        str(item) for item in normalize_int_list(value, field_name=field_name)
    )
