#!/usr/bin/env python3
"""Deploy-time reconciliation for assistant control-plane work."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from collections.abc import Sequence

logger = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile Droid deployment control-plane state before assistant wake.",
    )
    parser.add_argument(
        "--environment",
        required=True,
        choices=["development", "staging", "production"],
        help="Target environment. Must match ORCHESTRA_URL.",
    )
    parser.add_argument(
        "--planes",
        default="control-plane",
        help=(
            "Comma-separated planes. Cloud Build uses control-plane only; runtime "
            "is reserved for explicit repair/prewarm runs with per-assistant identity."
        ),
    )
    parser.add_argument(
        "--client",
        default=None,
        help="Optional client filter, e.g. client_alpha.",
    )
    parser.add_argument(
        "--assistant-id",
        default=None,
        help="Optional assistant-id filter for a single assistant target.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Maximum concurrent work items.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failed work item.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned work without writing.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply planned work.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser.parse_args(argv)


def _validate_required_environment(requested_environment: str) -> None:
    missing = [
        name
        for name in ("ORCHESTRA_URL", "ORCHESTRA_ADMIN_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise ValueError(
            f"Missing required environment variable(s): {', '.join(missing)}",
        )

    from droid_deploy.assistant_deployments.deployment_types import detect_environment

    detected_environment = detect_environment()
    if detected_environment != requested_environment:
        raise ValueError(
            "Requested environment "
            f"{requested_environment!r} does not match ORCHESTRA_URL-derived "
            f"environment {detected_environment!r}.",
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from droid_deploy.deployment_reconcile import (
        build_deployment_work_items,
        execute_work_items,
        format_work_item,
        format_work_result,
        parse_planes,
    )

    try:
        planes = parse_planes(args.planes)
        _validate_required_environment(args.environment)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    items = build_deployment_work_items(
        environment=args.environment,
        planes=planes,
        client=args.client,
        assistant_id=args.assistant_id,
    )

    if not items:
        logger.info("No deployment reconciliation work planned.")
        return 0

    for item in items:
        logger.info("Planned: %s", format_work_item(item))

    results = execute_work_items(
        items,
        apply=args.apply,
        concurrency=args.concurrency,
        fail_fast=args.fail_fast,
    )

    counts = Counter(result.status for result in results)
    for result in results:
        logger.info("Result: %s", format_work_result(result))
    logger.info(
        "Deployment reconciliation summary: %s",
        ", ".join(f"{status}={count}" for status, count in sorted(counts.items())),
    )

    if any(result.status == "failed" for result in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
