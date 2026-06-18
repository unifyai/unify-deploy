#!/usr/bin/env python3
"""Ingest worker entrypoint.

Consumes IngestRequested messages from Pub/Sub, reads ParsedFileBundle manifests
from GCS, and streams rows into DataManager.

Ingest workers are shared across many users / assistants, so the pod
does **not** carry a per-assistant ``UNIFY_KEY``. Instead, each
message's :class:`IngestBinding` drives a per-message api_key lookup
via Orchestra's admin endpoints (see
``unity_deploy.infra.workers.assistant_key_resolver``). The resolved
key is installed into ``os.environ["UNIFY_KEY"]`` just before the
Unify SDK is touched and removed on exit, so a leaked key never
bleeds into heartbeat or shutdown paths.

Usage (local-with-GCP testing):
    python -m unity_deploy.infra.workers.entrypoint_ingest --project Assistants

Environment (required):
    ORCHESTRA_URL, ORCHESTRA_ADMIN_KEY

Environment (GCP):
    GCP_SA_KEY, DROID_GCP_PIPELINE_ENVIRONMENT, DROID_PUBSUB_PROJECT_ID,
    DROID_GCS_ARTIFACT_BUCKET

Environment (optional):
    UNIFY_PROJECT_NAME
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import random
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
_current_receipt: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_ingest_receipt",
    default=None,
)


def _parse_expiry_seconds(expires_at: str) -> float | None:
    if not expires_at:
        return None
    text = expires_at.replace("Z", "+00:00")
    try:
        expires = datetime.fromisoformat(text)
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return (expires - datetime.now(timezone.utc)).total_seconds()


def _duplicate_defer_seconds(expires_at: str) -> int:
    default_seconds = int(os.environ.get("DROID_DUPLICATE_DEFER_SECONDS", "300"))
    max_seconds = int(os.environ.get("DROID_DUPLICATE_DEFER_MAX_SECONDS", "600"))
    jitter_seconds = int(os.environ.get("DROID_DUPLICATE_DEFER_JITTER_SECONDS", "30"))
    until_expiry = _parse_expiry_seconds(expires_at)
    if until_expiry is None:
        base = default_seconds
    else:
        # Wake shortly after the current GCS owner lease should have expired.
        base = max(0, int(until_expiry) + 5)
    if base > 0 and jitter_seconds > 0:
        base += random.randint(0, jitter_seconds)
    return max(0, min(base, max_seconds))


async def main() -> None:
    import argparse

    from droid.common.pipeline.work_queue import RetryWorkItem

    from .ingest_worker import handle_ingest_message
    from .pipeline_events import record_worker_event
    from .worker_utils import (
        DuplicateLiveAttempt,
        LeaseExtender,
        build_worker_infra,
        initialize_worker_environment,
        install_signal_handlers,
        is_shutdown_requested,
        shutdown_aware_sleep,
    )

    parser = argparse.ArgumentParser(description="Droid ingest worker")
    parser.add_argument(
        "--project",
        default="Assistants",
        help="Unify project name (default: Assistants)",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    initialize_worker_environment(debug=args.debug)
    install_signal_handlers()
    # No boot-time `activate_unify_context` call: the worker is shared
    # across many assistants and has no identity of its own. Per-message
    # activation lives in ``ingest_worker._with_unify_key`` which
    # resolves the caller's ``UNIFY_KEY`` from Orchestra and activates
    # the right (user_id, assistant_id) context before the Unify SDK is
    # touched.
    _ = args.project  # kept for CLI compatibility; project read per-msg.

    infra = build_worker_infra()

    logger.info("Ingest worker started, polling for messages...")

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
                    topics=["ingest"],
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
                    receipt_token = _current_receipt.set(item.receipt_id)
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
                        stage="ingest" if heartbeat_ledger else None,
                    )
                    lease_extender.start()
                    lease_outcome = "error"
                    record_worker_event(
                        infra,
                        item,
                        event_type="message_received",
                        stage="ingest",
                        next_action="handle_ingest_message",
                    )
                    try:
                        acked = await handle_ingest_message(
                            item,
                            infra=infra,
                            ack_receipt=lambda: (
                                record_worker_event(
                                    infra,
                                    item,
                                    event_type="message_ack_requested",
                                    stage="ingest",
                                    next_action="ack",
                                    metadata={"source": "handler"},
                                )
                                or infra.work_queue.ack(item.receipt_id)
                            ),
                        )
                        if not acked:
                            record_worker_event(
                                infra,
                                item,
                                event_type="message_ack_requested",
                                stage="ingest",
                                next_action="ack",
                            )
                            await infra.work_queue.ack(item.receipt_id)
                        lease_outcome = "ack"
                    except DuplicateLiveAttempt as exc:
                        lease = exc.lease
                        max_deferrals = int(
                            os.environ.get("DROID_DUPLICATE_DEFER_MAX_ATTEMPTS", "12"),
                        )
                        delivery_attempt = int(item.delivery_attempt or 0)
                        defer_seconds = _duplicate_defer_seconds(
                            str(getattr(lease, "expires_at", "") or ""),
                        )
                        if delivery_attempt >= max_deferrals:
                            record_worker_event(
                                infra,
                                item,
                                event_type="duplicate_live_attempt_dead_lettered",
                                stage="ingest",
                                error=str(exc),
                                next_action="dead_letter_duplicate",
                                metadata={
                                    "active_owner": getattr(lease, "owner_id", ""),
                                    "active_expires_at": getattr(
                                        lease,
                                        "expires_at",
                                        "",
                                    ),
                                    "delivery_attempt": delivery_attempt,
                                    "max_deferrals": max_deferrals,
                                },
                            )
                            await infra.work_queue.dead_letter(
                                item.receipt_id,
                                error=(
                                    "duplicate live attempt deferral budget exhausted: "
                                    f"{exc}"
                                ),
                            )
                            lease_outcome = "ack"
                            continue
                        record_worker_event(
                            infra,
                            item,
                            event_type="duplicate_live_attempt_deferred",
                            stage="ingest",
                            error=str(exc),
                            next_action="defer_duplicate",
                            metadata={
                                "active_owner": getattr(lease, "owner_id", ""),
                                "active_expires_at": getattr(lease, "expires_at", ""),
                                "delay_seconds": defer_seconds,
                                "delivery_attempt": delivery_attempt,
                                "max_deferrals": max_deferrals,
                            },
                        )
                        await infra.work_queue.retry(
                            item.receipt_id,
                            error=str(exc),
                            delay_seconds=defer_seconds,
                        )
                        lease_outcome = "error"
                    except RetryWorkItem as exc:
                        record_worker_event(
                            infra,
                            item,
                            event_type="message_retry_requested",
                            stage="ingest",
                            error=str(exc),
                            next_action="nack",
                            metadata={"delay_seconds": exc.delay_seconds},
                        )
                        await infra.work_queue.retry(
                            item.receipt_id,
                            error=str(exc),
                            delay_seconds=exc.delay_seconds,
                        )
                        lease_outcome = "error"
                    except Exception as exc:
                        logger.exception("Ingest message failed")
                        record_worker_event(
                            infra,
                            item,
                            event_type="app_dead_letter_requested",
                            stage="ingest",
                            error=str(exc),
                            next_action="dead_letter",
                        )
                        await infra.work_queue.dead_letter(
                            item.receipt_id,
                            error=str(exc),
                        )
                        lease_outcome = "ack"
                    finally:
                        lease_extender.stop(outcome=lease_outcome)
                        if heartbeat_ledger is not None:
                            try:
                                heartbeat_ledger.close()
                            except Exception:
                                logger.exception(
                                    "heartbeat_ledger.close() failed " "for run=%s",
                                    job_id,
                                )
                        _current_receipt.reset(receipt_token)

            except Exception:
                logger.exception("Ingest worker loop error")
                if await shutdown_aware_sleep(5.0):
                    break
    finally:
        logger.info("Ingest worker draining: closing work queue")
        try:
            await infra.work_queue.close()
        except Exception:
            logger.exception("work_queue.close() failed during shutdown")
        logger.info("Ingest worker shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
