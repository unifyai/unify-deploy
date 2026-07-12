"""Helpers for deployment-defined ``knowledge.jsonl`` claim sources."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

from unify.knowledge_manager.custom_knowledge import KNOWLEDGE_JSONL_FILENAME


def write_knowledge_jsonl(
    directory: Path,
    entries: Iterable[Mapping[str, object]],
) -> Path:
    """Write ``knowledge.jsonl`` claim entries and return the directory path."""
    directory.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(dict(entry), ensure_ascii=False) for entry in entries]
    (directory / KNOWLEDGE_JSONL_FILENAME).write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    return directory


def write_knowledge_from_spec(
    directory: Path,
    claims: Iterable[Mapping[str, object]] | Mapping[str, Mapping[str, object]],
) -> Path:
    """Materialize inline claim specs into a knowledge root directory.

    Accepts either an iterable of claim dicts (each must include ``key``) or a
    mapping of ``key`` -> claim fields (``key`` is injected when missing).
    """
    if isinstance(claims, Mapping):
        entries = []
        for key, spec in claims.items():
            entry = dict(spec)
            entry.setdefault("key", key)
            entries.append(entry)
    else:
        entries = [dict(spec) for spec in claims]
    return write_knowledge_jsonl(directory, entries)
