"""Helpers for deployment-defined DataManager table directories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from unify.data_manager.custom_data import (
    META_JSON_FILENAME,
    ROWS_JSONL_FILENAME,
)


def write_data_table(
    directory: Path,
    context: str,
    *,
    description: str = "",
    fields: dict[str, str] | None = None,
    seed_key: str,
    rows: Iterable[dict[str, object]],
    destination: str = "personal",
    unique_keys: dict[str, str] | None = None,
    auto_counting: dict[str, str | None] | None = None,
) -> Path:
    """Write one custom data table directory and return its path."""
    table_dir = directory
    for part in context.split("/"):
        table_dir = table_dir / part
    table_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, object] = {
        "description": description,
        "fields": fields or {},
        "seed_key": seed_key,
        "destination": destination,
    }
    if unique_keys is not None:
        meta["unique_keys"] = unique_keys
    if auto_counting is not None:
        meta["auto_counting"] = auto_counting
    (table_dir / META_JSON_FILENAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    row_lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    (table_dir / ROWS_JSONL_FILENAME).write_text(
        "\n".join(row_lines) + ("\n" if row_lines else ""),
        encoding="utf-8",
    )
    return table_dir


def write_data_from_spec(
    directory: Path,
    tables: dict[str, dict[str, object]],
) -> Path:
    """Materialize inline data table specs into a custom data root directory."""
    directory.mkdir(parents=True, exist_ok=True)
    for context, spec in tables.items():
        write_data_table(
            directory,
            context,
            description=str(spec.get("description", "")),
            fields=dict(spec.get("fields", spec.get("columns", {}))),
            seed_key=str(spec["seed_key"]),
            rows=list(spec.get("rows", [])),
            destination=str(spec.get("destination", "personal")),
            unique_keys=dict(spec["unique_keys"]) if spec.get("unique_keys") else None,
            auto_counting=(
                dict(spec["auto_counting"]) if spec.get("auto_counting") else None
            ),
        )
    return directory
