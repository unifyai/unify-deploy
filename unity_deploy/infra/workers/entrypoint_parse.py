#!/usr/bin/env python3
"""Parse worker entrypoint.

Consumes ParseRequested messages from Pub/Sub and runs FileParser.parse_batch()
for each message, writing results to GCS and publishing IngestRequested.

Parse workers do **not** call the Unify SDK -- they only need GCS and
Pub/Sub access. ``UNIFY_KEY`` / ``USER_ID`` / ``ASSISTANT_ID`` are
deliberately absent from both the pod manifest and this entrypoint:
the per-message api_key lookup lives on the ingest worker
(see ``unity_deploy.infra.workers.ingest_worker._with_unify_key``).

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
        LeaseExtender,
        build_worker_infra,
        initialize_worker_environment,
        install_signal_handlers,
        is_shutdown_requested,
        shutdown_aware_sleep,
    )

    parser = argparse.ArgumentParser(description="Unity parse worker")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    initialize_worker_environment(debug=args.debug)
    install_signal_handlers()

    infra = build_worker_infra()

    logger.info("Parse worker started, polling for messages...")

    consecutive_empty = 0
    try:
        while not is_shutdown_requested():
            try:
                # Pre-pull shutdown check: belt-and-suspenders against the
                # race where SIGTERM arrives between the while-guard and
                # the receive call. Once past this point we own any
                # message we pull and will finish it (24h grace gives us
                # the headroom rather than re-delivering to another pod).
                if is_shutdown_requested():
                    break

                items = await infra.work_queue.receive(
                    max_messages=1,
                    topics=["parse"],
                )

                if not items:
                    consecutive_empty += 1
                    wait = min(2.0 * consecutive_empty, 30.0)
                    # Shutdown-aware sleep wakes immediately on SIGTERM
                    # instead of blocking up to 30s in the empty-queue
                    # backoff path.
                    if await shutdown_aware_sleep(wait):
                        break
                    continue

                consecutive_empty = 0

                for item in items:
                    # Extract the job_id (== run_id) from the payload so we
                    # can (a) tag lease-heartbeat log lines with the run,
                    # and (b) construct a per-run heartbeat ledger. Empty
                    # job_id → skip ledger persistence (legacy payloads).
                    job_id = str(item.payload.get("job_id") or "")
                    heartbeat_ledger = None
                    if job_id:
                        try:
                            heartbeat_ledger = infra.heartbeat_ledger_factory(
                                job_id,
                            )
                        except Exception:
                            # A heartbeat-ledger init failure must never
                            # block message processing: we fall back to
                            # log-only liveness and continue.
                            logger.exception(
                                "Failed to create heartbeat ledger "
                                "for run=%s; continuing without "
                                "persisted heartbeats",
                                job_id,
                            )
                    lease_extender = LeaseExtender(
                        work_queue=infra.work_queue,
                        receipt_id=item.receipt_id,
                        job_id=job_id or None,
                        run_ledger=heartbeat_ledger,
                        run_id=job_id or None if heartbeat_ledger else None,
                        stage="parse" if heartbeat_ledger else None,
                    )
                    lease_extender.start()
                    lease_outcome = "error"
                    try:
                        await handle_parse_message(item, infra=infra)
                        await infra.work_queue.ack(item.receipt_id)
                        lease_outcome = "ack"
                    except RetryWorkItem as exc:
                        await infra.work_queue.retry(
                            item.receipt_id,
                            error=str(exc),
                            delay_seconds=exc.delay_seconds,
                        )
                        lease_outcome = "error"
                    except Exception as exc:
                        logger.exception("Parse message failed")
                        await infra.work_queue.dead_letter(
                            item.receipt_id,
                            error=str(exc),
                        )
                        lease_outcome = "ack"
                    finally:
                        if is_shutdown_requested() and lease_outcome == "error":
                            lease_outcome = "nack"
                        lease_extender.stop(outcome=lease_outcome)
                        if heartbeat_ledger is not None:
                            try:
                                heartbeat_ledger.close()
                            except Exception:
                                logger.exception(
                                    "heartbeat_ledger.close() failed " "for run=%s",
                                    job_id,
                                )

            except Exception:
                logger.exception("Parse worker loop error")
                if await shutdown_aware_sleep(5.0):
                    break
    finally:
        logger.info("Parse worker draining: closing work queue")
        try:
            await infra.work_queue.close()
        except Exception:
            logger.exception("work_queue.close() failed during shutdown")
        logger.info("Parse worker shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
