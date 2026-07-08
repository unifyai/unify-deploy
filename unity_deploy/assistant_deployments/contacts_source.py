"""Helpers for deployment-defined ``contacts.jsonl`` sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from unify.contact_manager.custom_contacts import (
    CONTACTS_JSONL_FILENAME,
    contact_entry_key,
)


def write_contacts_jsonl(
    directory: Path,
    entries: Iterable[dict[str, object]],
) -> Path:
    """Write ``contacts.jsonl`` and return the directory path."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
    (directory / CONTACTS_JSONL_FILENAME).write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    return directory


def entry_from_fields(
    *,
    first_name: str,
    surname: str,
    destination: str = "personal",
    **fields: object,
) -> dict[str, object]:
    """Build one contacts.jsonl row with a stable key."""
    return {
        "key": contact_entry_key(first_name=first_name, surname=surname),
        "first_name": first_name,
        "surname": surname,
        "destination": destination,
        **fields,
    }
