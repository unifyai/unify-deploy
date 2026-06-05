"""Sibling-repo registry plus branch-name → preview-slug normalization.

The preview workflow assumes each sibling repository lives next to
``unity-deploy`` under the user's local ``Unify`` directory.  The ``Repo``
dataclass is the unit of "a checkout we touch via git": it introspects the
local branch state and pushes / deletes feature branches on the corresponding
origin remote without disturbing the caller's working tree.

Slugging rules match those baked into the Cloud Build YAMLs so the
slug computed locally for ``preview status`` matches the slug the
build pipelines compute from ``${BRANCH_NAME}``.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
"""Default sibling-repo root.  ``unity-deploy`` lives at
``<root>/unity-deploy`` and the others at peer paths."""

FEATURE_BRANCH_PREFIX = "feature/"
SLUG_MAX_LENGTH = 30
DEFAULT_BASE_BRANCH = "staging"
"""Branch every preview pass-through is sourced from.  Each sibling
repo in this stack uses ``staging`` as its mainline integration branch;
the CLI fetches ``origin/<DEFAULT_BASE_BRANCH>`` before publishing a
pass-through ref so the new feature branch starts from the latest
shared tip."""


class GitError(RuntimeError):
    """Raised when a git command run against a sibling repo fails."""


@dataclass(frozen=True)
class Repo:
    """One sibling repository participating in the preview workflow."""

    name: str
    description: str

    def path(self, root: Path = REPO_ROOT) -> Path:
        return root / self.name

    def current_branch(self, root: Path = REPO_ROOT) -> str | None:
        """Return the checked-out branch name, or ``None`` if not a git repo."""
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.path(root),
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        return out.stdout.strip() or None

    def remote_branch_exists(
        self,
        branch: str,
        root: Path = REPO_ROOT,
    ) -> bool:
        """Return whether ``origin`` already publishes ``branch``."""
        result = self._run_git(
            ["ls-remote", "--exit-code", "--heads", "origin", branch],
            root=root,
            check=False,
        )
        return result.returncode == 0

    def push_passthrough_branch(
        self,
        feature_branch: str,
        *,
        base_branch: str = DEFAULT_BASE_BRANCH,
        force: bool = False,
        root: Path = REPO_ROOT,
    ) -> None:
        """Publish ``feature_branch`` on origin pointed at ``base_branch``'s tip.

        Fetches the latest ``origin/<base_branch>`` first and then pushes
        that ref to ``refs/heads/<feature_branch>`` without touching the
        caller's working tree, local branches, or HEAD.  ``force`` allows
        overwriting an existing remote branch (use sparingly — typically
        only to re-base a stale pass-through against fresh staging).
        """
        self._run_git(["fetch", "origin", base_branch], root=root)
        refspec = f"origin/{base_branch}:refs/heads/{feature_branch}"
        cmd = ["push"]
        if force:
            cmd.append("--force")
        cmd.extend(["origin", refspec])
        self._run_git(cmd, root=root)

    def delete_remote_branch(
        self,
        feature_branch: str,
        root: Path = REPO_ROOT,
    ) -> bool:
        """Delete ``feature_branch`` from origin if it exists.

        Returns ``True`` when a branch was actually removed.  Local
        branches and the working tree are untouched.
        """
        if not self.remote_branch_exists(feature_branch, root=root):
            return False
        self._run_git(["push", "origin", "--delete", feature_branch], root=root)
        return True

    def _run_git(
        self,
        args: list[str],
        *,
        root: Path = REPO_ROOT,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        repo_path = self.path(root)
        if not repo_path.exists():
            raise GitError(f"{self.name}: repo directory not found at {repo_path}")
        try:
            return subprocess.run(
                ["git", *args],
                cwd=repo_path,
                check=check,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise GitError(
                f"{self.name}: git {' '.join(args)} failed: {stderr}",
            ) from exc


REPOS: tuple[Repo, ...] = (
    Repo("orchestra", "FastAPI backend + assistant DB"),
    Repo("unity", "Assistant runtime (in-container brain)"),
    Repo("unity-deploy", "Hosted deploy, Comms App, and Adapters control plane"),
    Repo("console", "Next.js frontend"),
)

PASSTHROUGH_REPO_NAMES: tuple[str, ...] = ("unity-deploy", "unity", "console")
"""Sibling repos whose Cloud Build triggers must be hit for an end-to-end
preview to come alive.

``unity-deploy`` produces the tagged Comms App and Adapters revisions
and still owns the Unity deployment overlay triggered by Unity preview
builds.  ``unity`` builds the per-branch Unity image and uploads its hash to
GCS.  ``console`` deploys the tagged frontend whose ``NEXT_PUBLIC_*`` peer URLs
are pinned to the matching tagged comms-app and adapters revisions.

``orchestra`` is omitted because its preview build is opt-in and rarely needed; schema
migrations are pushed through the regular staging flow."""


def slugify(branch: str) -> str:
    """Reduce a branch name to a Cloud Run tag-safe slug.

    Mirrors the sed pipeline in the preview cloudbuild YAMLs:
    strip the ``feature/`` prefix, lowercase, collapse runs of
    non-``[a-z0-9-]`` characters into single hyphens, trim leading
    and trailing hyphens, and cap length at ``SLUG_MAX_LENGTH``.
    """
    stripped = branch.removeprefix(FEATURE_BRANCH_PREFIX).lower()
    sanitized = re.sub(r"[^a-z0-9-]+", "-", stripped).strip("-")
    return sanitized[:SLUG_MAX_LENGTH].rstrip("-")
