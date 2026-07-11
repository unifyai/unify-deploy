"""Helpers for deployment-defined ``blacklist.jsonl`` sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from unify.blacklist_manager.custom_blacklist import (
    BLACKLIST_JSONL_FILENAME,
    blacklist_entry_key,
)


def write_blacklist_jsonl(
    directory: Path,
    entries: Iterable[dict[str, object]],
) -> Path:
    """Write ``blacklist.jsonl`` and return the directory path."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
    (directory / BLACKLIST_JSONL_FILENAME).write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    return directory


def entry_from_fields(
    *,
    medium: str,
    contact_detail: str,
    reason: str,
    destination: str = "personal",
) -> dict[str, object]:
    """Build one blacklist.jsonl row with a stable key."""
    return {
        "key": blacklist_entry_key(medium=medium, contact_detail=contact_detail),
        "medium": medium,
        "contact_detail": contact_detail,
        "reason": reason,
        "destination": destination,
    }
