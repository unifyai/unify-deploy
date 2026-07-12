"""Helpers for deployment-defined ``tasks.jsonl`` sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from unify.task_scheduler.custom_tasks import TASKS_JSONL_FILENAME


def write_tasks_jsonl(
    directory: Path,
    entries: Iterable[dict[str, object]],
) -> Path:
    """Write ``tasks.jsonl`` and return the directory path."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
    (directory / TASKS_JSONL_FILENAME).write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    return directory
