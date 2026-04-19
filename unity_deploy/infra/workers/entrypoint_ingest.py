#!/usr/bin/env python3
"""Ingest worker entrypoint.

Consumes IngestRequested messages from Pub/Sub, reads ParsedFileBundle manifests
from GCS, and streams rows into DataManager.

Ingest workers need Unify SDK access (``DataManager.ingest`` writes to
Unify tables), so ``UNIFY_KEY``, ``USER_ID``, and ``ASSISTANT_ID`` must
be set.  Only a lightweight context activation is performed -- no full
``unity.init()`` (EventBus, LLM hooks, etc.) is required.

Usage (local-with-GCP testing):
    python -m unity_deploy.infra.workers.entrypoint_ingest --project Assistants

Environment (required):
    UNIFY_KEY, USER_ID, ASSISTANT_ID

Environment (GCP):
    GCP_SA_KEY, UNITY_GCP_PIPELINE_ENVIRONMENT, UNITY_PUBSUB_PROJECT_ID,
    UNITY_GCS_ARTIFACT_BUCKET

Environment (optional):
    UNIFY_PROJECT_NAME
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def main() -> None:
    import argparse

    from unity.common.pipeline.work_queue import RetryWorkItem

    from .ingest_worker import handle_ingest_message
    from .worker_utils import (
        activate_unify_context,
        build_worker_infra,
        initialize_worker_environment,
        install_signal_handlers,
        is_shutdown_requested,
    )

    parser = argparse.ArgumentParser(description="Unity ingest worker")
    parser.add_argument(
        "--project",
        default="Assistants",
        help="Unify project name (default: Assistants)",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    initialize_worker_environment(debug=args.debug)
    install_signal_handlers()
    activate_unify_context(args.project)

    infra = build_worker_infra()

    logger.info("Ingest worker started, polling for messages...")

    consecutive_empty = 0
    while not is_shutdown_requested():
        try:
            items = await infra.work_queue.receive(
                max_messages=1,
                topics=["ingest"],
            )

            if not items:
                consecutive_empty += 1
                wait = min(2.0 * consecutive_empty, 30.0)
                await asyncio.sleep(wait)
                continue

            consecutive_empty = 0

            for item in items:
                try:
                    await handle_ingest_message(item, infra=infra)
                    await infra.work_queue.ack(item.receipt_id)
                except RetryWorkItem as exc:
                    await infra.work_queue.retry(
                        item.receipt_id,
                        error=str(exc),
                        delay_seconds=exc.delay_seconds,
                    )
                except Exception as exc:
                    logger.exception("Ingest message failed")
                    await infra.work_queue.dead_letter(
                        item.receipt_id,
                        error=str(exc),
                    )

        except Exception:
            logger.exception("Ingest worker loop error")
            await asyncio.sleep(5.0)

    logger.info("Ingest worker shutting down gracefully")


if __name__ == "__main__":
    asyncio.run(main())
