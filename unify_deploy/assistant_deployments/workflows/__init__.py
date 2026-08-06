"""The curated workflow catalogue: one directory per installable bundle.

Each bundle follows the integration-package layout — ``manifest.yaml``
plus the content directories (``guidance/``, ``knowledge/``, ``tasks/``,
``functions/``) — and is loaded by unify's workflow catalogue at boot
when ``UNITY_WORKFLOWS_DIR`` points here (see ``workflows_root``).

Authoring is git-only: bundles are hand-curated, reviewed in PRs, and a
version bump here reaches every existing installation on its next
session start. The bundle's ``slug`` must match its directory name — it
is stamped as ``managed_by`` on every row the workflow plants, so a
rename is an identity migration for every installation, never a move.
"""

from pathlib import Path


def workflows_root() -> Path:
    """Absolute path of the catalogue, for UNITY_WORKFLOWS_DIR."""
    return Path(__file__).resolve().parent
