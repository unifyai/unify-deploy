"""Entry point for ``python -m scripts.preview``."""

from __future__ import annotations

import argparse
import sys

from .commands import (
    DEFAULT_CLEANUP_AGE_DAYS,
    cleanup_command,
    down_command,
    open_command,
    status_command,
    up_command,
    url_command,
)
from .repos import DEFAULT_BASE_BRANCH


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preview",
        description=(
            "Inspect and manage feature-branch preview deployments across "
            "Console, Orchestra, Comms App, Adapters, and Unity images."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status",
        help="Show repo branches plus tagged-revision deployment status",
    )
    status.add_argument(
        "--slug",
        help="Inspect a specific slug instead of inferring it from feature branches",
    )

    url = sub.add_parser("url", help="Print the tagged Console URL for a slug")
    url.add_argument("--slug", help="Override the slug derived from local branches")

    open_cmd = sub.add_parser(
        "open",
        help="Open the tagged Console URL in the default browser",
    )
    open_cmd.add_argument(
        "--slug",
        help="Override the slug derived from local branches",
    )

    up = sub.add_parser(
        "up",
        help=(
            "Publish pass-through feature/<slug> branches on origin in "
            "every preview-relevant sibling repo so their Cloud Build "
            "triggers fire."
        ),
    )
    up.add_argument(
        "--slug",
        help="Override the slug derived from local branches",
    )
    up.add_argument(
        "--base-branch",
        default=DEFAULT_BASE_BRANCH,
        help=(
            f"Branch each pass-through ref is sourced from "
            f"(default: {DEFAULT_BASE_BRANCH})"
        ),
    )
    up.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-push existing pass-throughs from the latest base-branch "
            "tip; use sparingly."
        ),
    )

    down = sub.add_parser(
        "down",
        help=(
            "Delete pass-through feature/<slug> branches from origin in "
            "every preview-relevant sibling repo."
        ),
    )
    down.add_argument(
        "--slug",
        help="Override the slug derived from local branches",
    )

    cleanup = sub.add_parser(
        "cleanup",
        help="Remove tagged Cloud Run revisions and Unity image-hash blobs older than --age-days",
    )
    cleanup.add_argument(
        "--age-days",
        type=float,
        default=DEFAULT_CLEANUP_AGE_DAYS,
        help=f"Age threshold in days (default: {DEFAULT_CLEANUP_AGE_DAYS})",
    )
    cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be removed without making changes",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return status_command(slug=args.slug)
    if args.command == "url":
        return url_command(slug=args.slug)
    if args.command == "open":
        return open_command(slug=args.slug)
    if args.command == "up":
        return up_command(
            slug=args.slug,
            base_branch=args.base_branch,
            force=args.force,
        )
    if args.command == "down":
        return down_command(slug=args.slug)
    if args.command == "cleanup":
        return cleanup_command(age_days=args.age_days, dry_run=args.dry_run)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
