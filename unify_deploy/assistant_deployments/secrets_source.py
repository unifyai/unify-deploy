"""Helpers for deployment-defined ``secrets.jsonl`` sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from unify.secret_manager.custom_secrets import (
    SECRETS_JSONL_FILENAME,
    secret_entry_key,
)


def write_secrets_jsonl(
    directory: Path,
    entries: Iterable[dict[str, object]],
) -> Path:
    """Write ``secrets.jsonl`` and return the directory path."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
    (directory / SECRETS_JSONL_FILENAME).write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    return directory


def entry_from_fields(
    *,
    name: str,
    value: str,
    description: str = "",
    destination: str = "personal",
) -> dict[str, object]:
    """Build one secrets.jsonl row with a stable key."""
    return {
        "key": secret_entry_key(name=name),
        "name": name,
        "value": value,
        "description": description,
        "destination": destination,
    }
