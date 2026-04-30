#!/usr/bin/env python3
"""Reconcile deploy-time control-plane state into Orchestra.

This is for durable metadata that must exist before a Unity assistant wakes,
such as assistant-scoped ``console_config`` for Console layouts.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

logger = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile client deployment control-plane state into Orchestra.",
    )
    parser.add_argument(
        "--environment",
        required=True,
        choices=["development", "staging", "production"],
        help="Target Orchestra environment. Must match ORCHESTRA_URL.",
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
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned operations without writing to Orchestra.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply planned operations to Orchestra.",
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

    from unity_deploy.customization.deployment_types import detect_environment

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

    try:
        _validate_required_environment(args.environment)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    from unity_deploy.control_plane.reconcile import (
        apply_operations,
        build_control_plane_plan,
        format_operation,
    )

    operations = build_control_plane_plan(
        environment=args.environment,
        client=args.client,
        assistant_id=args.assistant_id,
    )

    if not operations:
        logger.info("No control-plane operations planned.")
        return 0

    for operation in operations:
        logger.info("Planned: %s", format_operation(operation))

    if args.dry_run:
        logger.info("Dry-run complete; no Orchestra writes performed.")
        return 0

    try:
        responses = apply_operations(operations)
    except Exception:
        logger.exception("Control-plane reconciliation failed.")
        return 1

    logger.info("Applied %d control-plane operation(s).", len(responses))
    return 0


if __name__ == "__main__":
    sys.exit(main())
