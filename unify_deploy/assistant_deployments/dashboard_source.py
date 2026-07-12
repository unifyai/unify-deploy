"""Helpers for deployment-defined dashboard directories."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from unify.dashboard_manager.custom_dashboards import (
    LAYOUTS_NAMESPACE,
    META_JSON_FILENAME,
    ROWS_JSONL_FILENAME,
    TILES_NAMESPACE,
)


def write_dashboard_tile(
    directory: Path,
    tile_id: str,
    *,
    title: str,
    html_content: str = "",
    html_template: str | None = None,
    description: str = "",
    data_bindings: list[dict[str, object]] | None = None,
    data_binding: dict[str, object] | None = None,
    on_data: str = "",
    on_data_script: str | None = None,
    destination: str = "personal",
    data_scope: str = "dashboard",
    token: str | None = None,
) -> Path:
    """Write one dashboard tile directory and return its path."""
    table_dir = directory / TILES_NAMESPACE / tile_id
    table_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "seed_key": "id",
        "destination": destination,
        "data_scope": data_scope,
    }
    if description:
        meta["description"] = description
    (table_dir / META_JSON_FILENAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    row: dict[str, object] = {
        "id": tile_id,
        "title": title,
        "html_content": html_content or html_template or "",
        "description": description or None,
    }
    if data_bindings is not None:
        row["data_bindings"] = data_bindings
    elif data_binding is not None:
        row["data_binding"] = data_binding
    script = on_data_script if on_data_script is not None else on_data
    if script:
        row["on_data_script"] = script
    if token:
        row["token"] = token
    (table_dir / ROWS_JSONL_FILENAME).write_text(
        json.dumps(row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return table_dir


def write_dashboard_layout(
    directory: Path,
    layout_id: str,
    *,
    title: str,
    description: str = "",
    positions: Iterable[dict[str, object]],
    destination: str = "personal",
    token: str | None = None,
) -> Path:
    """Write one dashboard layout directory and return its path."""
    table_dir = directory / LAYOUTS_NAMESPACE / layout_id
    table_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "seed_key": "id",
        "destination": destination,
    }
    if description:
        meta["description"] = description
    (table_dir / META_JSON_FILENAME).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    row: dict[str, object] = {
        "id": layout_id,
        "title": title,
        "description": description or None,
        "positions": list(positions),
    }
    if token:
        row["token"] = token
    (table_dir / ROWS_JSONL_FILENAME).write_text(
        json.dumps(row, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return table_dir
