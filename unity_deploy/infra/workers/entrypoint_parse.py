#!/usr/bin/env python3
"""Parse worker entrypoint.

Consumes ParseRequested messages from Pub/Sub and runs FileParser.parse_batch()
for each message, writing results to GCS and publishing IngestRequested.

Parse workers do **not** call the Unify SDK -- they only need GCS and
Pub/Sub access.  No ``UNIFY_KEY``, ``USER_ID``, or ``ASSISTANT_ID``
required.

Usage (local-with-GCP testing):
    python -m unity_deploy.infra.workers.entrypoint_parse

Environment (GCP):
    GCP_SA_KEY, UNITY_GCP_PIPELINE_ENVIRONMENT, UNITY_PUBSUB_PROJECT_ID,
    UNITY_GCS_ARTIFACT_BUCKET
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def main() -> None:
    import argparse

    from unity.common.pipeline.work_queue import RetryWorkItem

    from .parse_worker import handle_parse_message
    from .worker_utils import (
        build_worker_infra,
        initialize_worker_environment,
        install_signal_handlers,
        is_shutdown_requested,
    )

    parser = argparse.ArgumentParser(description="Unity parse worker")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    initialize_worker_environment(debug=args.debug)
    install_signal_handlers()

    infra = build_worker_infra()

    logger.info("Parse worker started, polling for messages...")

    consecutive_empty = 0
    while not is_shutdown_requested():
        try:
            items = await infra.work_queue.receive(
                max_messages=1,
                topics=["parse"],
            )

            if not items:
                consecutive_empty += 1
                wait = min(2.0 * consecutive_empty, 30.0)
                await asyncio.sleep(wait)
                continue

            consecutive_empty = 0

            for item in items:
                try:
                    await handle_parse_message(item, infra=infra)
                    await infra.work_queue.ack(item.receipt_id)
                except RetryWorkItem as exc:
                    await infra.work_queue.retry(
                        item.receipt_id,
                        error=str(exc),
                        delay_seconds=exc.delay_seconds,
                    )
                except Exception as exc:
                    logger.exception("Parse message failed")
                    await infra.work_queue.dead_letter(
                        item.receipt_id,
                        error=str(exc),
                    )

        except Exception:
            logger.exception("Parse worker loop error")
            await asyncio.sleep(5.0)

    logger.info("Parse worker shutting down gracefully")


if __name__ == "__main__":
    asyncio.run(main())
