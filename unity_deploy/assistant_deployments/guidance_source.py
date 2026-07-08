"""Helpers for deployment-defined ``guidance.jsonl`` sources."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from unify.guidance_manager.custom_guidance import GUIDANCE_JSONL_FILENAME


def slugify_key(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def write_guidance_jsonl(
    directory: Path,
    entries: Iterable[dict[str, object]],
) -> Path:
    """Write ``guidance.jsonl`` and return the directory path."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
    (directory / GUIDANCE_JSONL_FILENAME).write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    return directory
