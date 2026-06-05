"""Thin wrappers around the ``gcloud`` and ``gsutil`` CLIs.

Keeps subprocess invocations in one place so the rest of the package
can use clean functions instead of plumbing argument lists.  All
helpers raise ``GcloudError`` on non-zero exits with the underlying
stderr inlined into the exception message.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone


class GcloudError(RuntimeError):
    """Raised when a ``gcloud``/``gsutil`` invocation fails."""


def _ensure(binary: str) -> None:
    if shutil.which(binary) is None:
        raise GcloudError(
            f"{binary!r} is not on PATH; install the Google Cloud SDK "
            f"and authenticate before running preview commands",
        )


def _run(cmd: list[str]) -> str:
    _ensure(cmd[0])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise GcloudError(
            f"command failed (exit {result.returncode}): "
            f"{' '.join(cmd)}\n{result.stderr.strip()}",
        )
    return result.stdout


@dataclass(frozen=True)
class TaggedRevision:
    """A single tagged revision on a Cloud Run service."""

    tag: str
    revision_name: str
    url: str
    created_at: datetime | None

    def age_days(self, now: datetime | None = None) -> float | None:
        if self.created_at is None:
            return None
        reference = now or datetime.now(timezone.utc)
        return (reference - self.created_at).total_seconds() / 86_400.0


def list_tagged_revisions(
    *,
    service: str,
    region: str,
    project: str,
) -> list[TaggedRevision]:
    """Return every tagged traffic target on a Cloud Run service."""
    raw = _run(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            service,
            f"--region={region}",
            f"--project={project}",
            "--format=json",
        ],
    )
    payload = json.loads(raw)
    targets = (payload.get("status") or {}).get("traffic") or []
    revisions: list[TaggedRevision] = []
    for target in targets:
        tag = target.get("tag")
        if not tag:
            continue
        revisions.append(
            TaggedRevision(
                tag=tag,
                revision_name=target.get("revisionName", ""),
                url=target.get("url", ""),
                created_at=_revision_created_at(
                    revision=target.get("revisionName", ""),
                    region=region,
                    project=project,
                ),
            ),
        )
    return revisions


def remove_tagged_revision(
    *,
    service: str,
    region: str,
    project: str,
    tag: str,
) -> None:
    """Remove a single tag from a Cloud Run service's traffic table."""
    _run(
        [
            "gcloud",
            "run",
            "services",
            "update-traffic",
            service,
            f"--region={region}",
            f"--project={project}",
            f"--remove-tags={tag}",
            "--quiet",
        ],
    )


def gcs_object_exists(*, gs_uri: str) -> bool:
    """Return whether a GCS object exists (uses ``gsutil ls``)."""
    _ensure("gsutil")
    result = subprocess.run(
        ["gsutil", "ls", gs_uri],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def gcs_read_text(*, gs_uri: str) -> str:
    """Return the contents of a GCS text object."""
    return _run(["gsutil", "cat", gs_uri]).strip()


def gcs_remove(*, gs_uri: str) -> None:
    """Delete a GCS object."""
    _run(["gsutil", "rm", gs_uri])


def _revision_created_at(
    *,
    revision: str,
    region: str,
    project: str,
) -> datetime | None:
    if not revision:
        return None
    try:
        raw = _run(
            [
                "gcloud",
                "run",
                "revisions",
                "describe",
                revision,
                f"--region={region}",
                f"--project={project}",
                "--format=value(metadata.creationTimestamp)",
            ],
        )
    except GcloudError:
        return None
    raw = raw.strip()
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))
