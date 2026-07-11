"""Helpers for deployment-defined knowledge table directories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from unify.knowledge_manager.custom_knowledge import (
    META_JSON_FILENAME,
    ROWS_JSONL_FILENAME,
)


def write_knowledge_table(
    directory: Path,
    table_name: str,
    *,
    description: str = "",
    columns: dict[str, str] | None = None,
    seed_key: str,
    rows: Iterable[dict[str, object]],
    destination: str = "personal",
) -> Path:
    """Write one knowledge table directory and return its path."""
    table_dir = directory
    for part in table_name.split("/"):
        table_dir = table_dir / part
    table_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "description": description,
        "columns": columns or {},
        "seed_key": seed_key,
        "destination": destination,
    }
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


def write_knowledge_from_spec(
    directory: Path,
    tables: dict[str, dict[str, object]],
) -> Path:
    """Materialize inline knowledge specs into a knowledge root directory."""
    directory.mkdir(parents=True, exist_ok=True)
    for table_name, spec in tables.items():
        write_knowledge_table(
            directory,
            table_name,
            description=str(spec.get("description", "")),
            columns=dict(spec.get("columns", {})),
            seed_key=str(spec["seed_key"]),
            rows=list(spec.get("rows", [])),
            destination=str(spec.get("destination", "personal")),
        )
    return directory
