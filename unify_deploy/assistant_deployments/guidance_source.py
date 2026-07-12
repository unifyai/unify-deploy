"""Helpers for deployment-defined ``guidance.jsonl`` sources."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

from unify.guidance_manager.custom_guidance import GUIDANCE_JSONL_FILENAME

# Deployments that regenerate guidance at call time (rather than shipping a
# static committed guidance.jsonl) need somewhere writable to put it: the
# installed package directory is read-only in reconcile jobs and bundled
# pods, so this lives under the OS temp dir instead.
_GENERATED_GUIDANCE_ROOT = (
    Path(
        os.environ.get("UNITY_DEPLOY_GUIDANCE_CACHE", tempfile.gettempdir()),
    )
    / "unity-deploy-guidance"
)


def slugify_key(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def generated_guidance_dir(namespace: str) -> Path:
    """Return a writable directory for guidance regenerated at call time.

    ``namespace`` should uniquely identify the deployment (for example
    ``"client_alpha/v2"``) so concurrent deployments don't clobber each
    other's ``guidance.jsonl``.
    """
    directory = _GENERATED_GUIDANCE_ROOT / namespace
    directory.mkdir(parents=True, exist_ok=True)
    return directory


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
