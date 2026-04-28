"""Command implementations behind the ``preview`` CLI.

``status`` prints the current branch in each sibling repo and reports
which preview-aware Cloud Run services and Unity image-hash blobs
have been deployed for the corresponding slug.

``url`` prints the tagged Console URL for a chosen slug.

``up`` publishes pass-through ``feature/<slug>`` branches on origin
for any sibling repo that does not already have one, so a single
real feature branch in one repo (for example ``unity``) can drive
the full preview pipeline without manually creating no-op branches
in the other repos.  ``down`` is its symmetric tear-down.

``cleanup`` removes tagged Cloud Run revisions and per-slug Unity
image-hash blobs older than a configurable age threshold.  Useful to
keep ``gcloud run services describe`` output readable after a busy
week of feature work.
"""

from __future__ import annotations

import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone

from .gcloud_client import (
    GcloudError,
    TaggedRevision,
    gcs_object_exists,
    gcs_read_text,
    gcs_remove,
    list_tagged_revisions,
    remove_tagged_revision,
)
from .repos import (
    DEFAULT_BASE_BRANCH,
    FEATURE_BRANCH_PREFIX,
    GitError,
    PASSTHROUGH_REPO_NAMES,
    REPOS,
    Repo,
    slugify,
)
from .services import (
    SERVICES,
    UNITY_IMAGE_HASH_BLOB,
    Service,
    service_by_name,
)

CONSOLE_SERVICE_NAME = "saas-web-app-redesign-staging"
DEFAULT_CLEANUP_AGE_DAYS = 14


@dataclass(frozen=True)
class SlugDeploymentStatus:
    """Snapshot of every preview artifact tied to a single slug."""

    slug: str
    service_revisions: dict[str, TaggedRevision | None]
    unity_image_hash: str | None


def status_command(*, slug: str | None = None) -> int:
    """Print branch info per repo and tagged-revision status per service."""
    print("Preview-environment status\n")
    print("Sibling repos:")
    inferred_slugs: set[str] = set()
    for repo in REPOS:
        branch = repo.current_branch()
        if branch is None:
            print(f"  {repo.name:<16} (not a git checkout)")
            continue
        repo_slug = slugify(branch) if branch.startswith("feature/") else ""
        slug_label = f"slug={repo_slug}" if repo_slug else "(non-feature branch)"
        print(f"  {repo.name:<16} {branch:<40} {slug_label}")
        if repo_slug:
            inferred_slugs.add(repo_slug)

    target_slug = slug or _single_or_none(inferred_slugs)
    if target_slug is None:
        print(
            "\nNo single feature-branch slug across repos; pass --slug to "
            "inspect a specific deployment.",
        )
        return 0

    snapshot = collect_status(target_slug)
    _print_status_snapshot(snapshot)
    return 0


def url_command(*, slug: str | None = None) -> int:
    """Print the tagged Console URL for a slug, opening the browser when set."""
    target_slug = slug or _slug_from_repos()
    if target_slug is None:
        print("Cannot determine a slug; pass --slug.", file=sys.stderr)
        return 2
    console = service_by_name(CONSOLE_SERVICE_NAME)
    if console is None:
        print("Console service is not registered.", file=sys.stderr)
        return 2
    print(console.tagged_url(target_slug))
    return 0


def open_command(*, slug: str | None = None) -> int:
    """Open the tagged Console URL in the default browser."""
    target_slug = slug or _slug_from_repos()
    if target_slug is None:
        print("Cannot determine a slug; pass --slug.", file=sys.stderr)
        return 2
    console = service_by_name(CONSOLE_SERVICE_NAME)
    if console is None:
        print("Console service is not registered.", file=sys.stderr)
        return 2
    url = console.tagged_url(target_slug)
    print(url)
    webbrowser.open(url)
    return 0


def up_command(
    *,
    slug: str | None = None,
    base_branch: str = DEFAULT_BASE_BRANCH,
    force: bool = False,
) -> int:
    """Ensure ``feature/<slug>`` exists on origin in every preview-relevant repo.

    Repos that already publish the branch are left alone — typically
    that is the repo holding the actual feature work.  Repos that do
    not are seeded with a pass-through ref pushed straight from
    ``origin/<base_branch>`` so their preview Cloud Build trigger
    fires without ever touching the caller's working tree.
    """

    target_slug = slug or _slug_from_repos()
    if target_slug is None:
        print(
            "Cannot determine a slug; pass --slug or check out a "
            "feature/<name> branch in one sibling repo.",
            file=sys.stderr,
        )
        return 2
    feature_branch = f"{FEATURE_BRANCH_PREFIX}{target_slug}"
    print(f"Ensuring {feature_branch} on origin for slug '{target_slug}':\n")

    failures = 0
    for repo in _passthrough_repos():
        try:
            if repo.remote_branch_exists(feature_branch):
                if force:
                    repo.push_passthrough_branch(
                        feature_branch,
                        base_branch=base_branch,
                        force=True,
                    )
                    print(
                        f"  [{repo.name:<14}] re-pushed from "
                        f"origin/{base_branch} (force)",
                    )
                else:
                    print(f"  [{repo.name:<14}] already exists, skipping")
                continue
            repo.push_passthrough_branch(
                feature_branch,
                base_branch=base_branch,
            )
            print(
                f"  [{repo.name:<14}] pushed from origin/{base_branch}",
            )
        except GitError as exc:
            failures += 1
            print(f"  [{repo.name:<14}] failed: {exc}", file=sys.stderr)

    if failures:
        print(f"\n{failures} repo(s) failed; see errors above.", file=sys.stderr)
        return 1
    print(
        f"\nDone.  Cloud Build triggers will now produce tagged revisions "
        f"and the per-slug Unity image-hash blob.  Use "
        f"'./scripts/preview.py status' once the builds finish.",
    )
    return 0


def down_command(*, slug: str | None = None) -> int:
    """Delete ``feature/<slug>`` from origin in every preview-relevant repo.

    Local branches and working trees are untouched; this only removes
    the remote refs whose preview Cloud Builds were producing tagged
    revisions for the slug.  Run ``cleanup`` afterwards to drop the
    actual deployed artifacts.
    """

    target_slug = slug or _slug_from_repos()
    if target_slug is None:
        print(
            "Cannot determine a slug; pass --slug.",
            file=sys.stderr,
        )
        return 2
    feature_branch = f"{FEATURE_BRANCH_PREFIX}{target_slug}"
    print(f"Removing {feature_branch} from origin for slug '{target_slug}':\n")

    failures = 0
    for repo in _passthrough_repos():
        try:
            removed = repo.delete_remote_branch(feature_branch)
        except GitError as exc:
            failures += 1
            print(f"  [{repo.name:<14}] failed: {exc}", file=sys.stderr)
            continue
        if removed:
            print(f"  [{repo.name:<14}] deleted from origin")
        else:
            print(f"  [{repo.name:<14}] not present on origin, skipping")

    if failures:
        print(f"\n{failures} repo(s) failed; see errors above.", file=sys.stderr)
        return 1
    print(
        "\nDone.  Tagged revisions remain in Cloud Run until the next "
        "'./scripts/preview.py cleanup' run.",
    )
    return 0


def cleanup_command(*, age_days: float, dry_run: bool) -> int:
    """Remove tagged revisions and image-hash blobs older than ``age_days``."""
    cutoff = datetime.now(timezone.utc)
    print(
        f"Cleanup target: tagged revisions older than {age_days:.1f} days"
        f"{' (dry-run)' if dry_run else ''}\n",
    )
    removed_revisions: list[tuple[Service, TaggedRevision]] = []
    for service in SERVICES:
        try:
            revisions = list_tagged_revisions(
                service=service.name,
                region=service.region,
                project=service.project,
            )
        except GcloudError as exc:
            print(f"  [{service.name}] skip: {exc}", file=sys.stderr)
            continue
        for revision in revisions:
            age = revision.age_days(cutoff)
            if age is None or age < age_days:
                continue
            removed_revisions.append((service, revision))
            print(
                f"  [{service.name}] tag={revision.tag} "
                f"age={age:.1f}d revision={revision.revision_name}",
            )
            if dry_run:
                continue
            try:
                remove_tagged_revision(
                    service=service.name,
                    region=service.region,
                    project=service.project,
                    tag=revision.tag,
                )
            except GcloudError as exc:
                print(f"    failed to remove tag: {exc}", file=sys.stderr)

    removed_blobs = _cleanup_image_hash_blobs(
        slugs={revision.tag for _, revision in removed_revisions},
        dry_run=dry_run,
    )

    print(
        f"\nDone.  {len(removed_revisions)} tagged revisions and "
        f"{removed_blobs} Unity image-hash blobs "
        f"{'would be' if dry_run else 'were'} removed.",
    )
    return 0


def collect_status(slug: str) -> SlugDeploymentStatus:
    """Query gcloud + gsutil for everything tied to a single slug."""
    revisions: dict[str, TaggedRevision | None] = {}
    for service in SERVICES:
        revisions[service.name] = _find_tagged(service=service, slug=slug)

    image_hash: str | None = None
    blob = UNITY_IMAGE_HASH_BLOB.gs_uri(slug)
    try:
        if gcs_object_exists(gs_uri=blob):
            image_hash = gcs_read_text(gs_uri=blob)
    except GcloudError:
        image_hash = None
    return SlugDeploymentStatus(
        slug=slug,
        service_revisions=revisions,
        unity_image_hash=image_hash,
    )


def _print_status_snapshot(snapshot: SlugDeploymentStatus) -> None:
    print(f"\nDeployment status for slug '{snapshot.slug}':\n")
    for service in SERVICES:
        revision = snapshot.service_revisions.get(service.name)
        if revision is None:
            line = "missing"
        else:
            line = f"deployed ({revision.revision_name})"
        print(f"  {service.name:<32} {line}")
    print(
        f"\n  unity image hash blob:           {snapshot.unity_image_hash or 'missing'}",
    )
    print()
    console = service_by_name(CONSOLE_SERVICE_NAME)
    if console is not None:
        print(f"Console URL:  {console.tagged_url(snapshot.slug)}")


def _find_tagged(*, service: Service, slug: str) -> TaggedRevision | None:
    try:
        revisions = list_tagged_revisions(
            service=service.name,
            region=service.region,
            project=service.project,
        )
    except GcloudError:
        return None
    for revision in revisions:
        if revision.tag == slug:
            return revision
    return None


def _cleanup_image_hash_blobs(*, slugs: set[str], dry_run: bool) -> int:
    removed = 0
    for slug in sorted(slugs):
        gs_uri = UNITY_IMAGE_HASH_BLOB.gs_uri(slug)
        try:
            if not gcs_object_exists(gs_uri=gs_uri):
                continue
        except GcloudError:
            continue
        print(f"  [unity-image-hash] {gs_uri}")
        if dry_run:
            removed += 1
            continue
        try:
            gcs_remove(gs_uri=gs_uri)
            removed += 1
        except GcloudError as exc:
            print(f"    failed to remove blob: {exc}", file=sys.stderr)
    return removed


def _slug_from_repos() -> str | None:
    candidates: set[str] = set()
    for repo in REPOS:
        branch = repo.current_branch()
        if branch and branch.startswith(FEATURE_BRANCH_PREFIX):
            candidates.add(slugify(branch))
    return _single_or_none(candidates)


def _single_or_none(items: set[str]) -> str | None:
    return next(iter(items)) if len(items) == 1 else None


def _passthrough_repos() -> list[Repo]:
    """Return the registered repos that own a preview Cloud Build trigger."""
    by_name = {repo.name: repo for repo in REPOS}
    return [by_name[name] for name in PASSTHROUGH_REPO_NAMES if name in by_name]
