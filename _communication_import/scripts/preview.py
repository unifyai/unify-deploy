#!/usr/bin/env python3
"""Convenience wrapper so ``./scripts/preview.py <command>`` works
without needing ``-m scripts.preview`` from the repo root.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.preview.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
