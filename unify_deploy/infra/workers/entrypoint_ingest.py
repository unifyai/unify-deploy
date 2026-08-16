#!/usr/bin/env python3
"""Ingest worker entrypoint.

Consumes IngestRequested messages from Pub/Sub, reads ParsedFileBundle manifests
from GCS, and streams rows into DataManager.

Ingest workers are shared across many users / assistants, so the pod
does **not** carry a per-assistant ``UNIFY_KEY``. Instead, each
message's :class:`IngestBinding` drives a per-message api_key lookup
via Orchestra's admin endpoints (see
``unify_deploy.infra.workers.assistant_key_resolver``). The resolved
key is installed into ``os.environ["UNIFY_KEY"]`` just before the
Unify SDK is touched and removed on exit, so a leaked key never
bleeds into heartbeat or shutdown paths.

Usage (local-with-GCP testing):
    python -m unify_deploy.infra.workers.entrypoint_ingest --project Assistants

Environment (required):
    ORCHESTRA_URL, ORCHESTRA_ADMIN_KEY

Environment (GCP):
    GCP_SA_KEY, UNITY_GCP_PIPELINE_ENVIRONMENT, UNITY_PUBSUB_PROJECT_ID,
    UNITY_GCS_ARTIFACT_BUCKET

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


def _lease_lifetime_cap() -> tuple[float | None, int | None]:
    """Resolve the lease-lifetime cap for in-flight ingest messages.

    Defense-in-depth so a single chunk can never hold a message forever
    (we observed 2.5h / 76 extensions), which is what made pause/stop
    unable to reclaim an in-flight chunk. The default (30 min) sits well
    above a healthy chunk time once the O(n^2) counter scan is gone; set
    ``UNIFY_INGEST_LEASE_MAX_LIFETIME_S=0`` to disable.
    """
    raw_lifetime = os.environ.get("UNIFY_INGEST_LEASE_MAX_LIFETIME_S", "1800")
    try:
        lifetime = float(raw_lifetime)
    except ValueError:
        lifetime = 1800.0
    max_lifetime_s = lifetime if lifetime > 0 else None

    raw_extensions = os.environ.get("UNIFY_INGEST_LEASE_MAX_EXTENSIONS", "")
    try:
        extensions = int(raw_extensions)
    except ValueError:
        extensions = 0
    max_extensions = extensions if extensions > 0 else None

    return max_lifetime_s, max_extensions


def _duplicate_defer_seconds(expires_at: str) -> int:
    # Short fallback when the holder's expiry is unreadable. A flat multi-minute
    # wait (the old 300s) blinds us to a lease that may already be stealable and
    # turns a ~30s wait into a 5-min stall; re-check soon instead, bounded by
    # UNIFY_DUPLICATE_DEFER_MAX_ATTEMPTS.
    fallback_seconds = int(
        os.environ.get("UNIFY_DUPLICATE_DEFER_FALLBACK_SECONDS", "30"),
    )
    max_seconds = int(os.environ.get("UNIFY_DUPLICATE_DEFER_MAX_SECONDS", "600"))
    jitter_seconds = int(os.environ.get("UNIFY_DUPLICATE_DEFER_JITTER_SECONDS", "30"))
    # An expired lease only becomes stealable after a grace window
    # (acquire_lease steal_expired_after_seconds, default 30s); add a small
    # buffer on top. Waking on the dot of expiry would land before the lease is
    # reclaimable and re-defer a whole cycle.
    steal_grace = int(os.environ.get("UNIFY_INGEST_LEASE_STEAL_GRACE_SECONDS", "30"))
    buffer_seconds = int(os.environ.get("UNIFY_DUPLICATE_DEFER_BUFFER_SECONDS", "5"))
    until_expiry = _parse_expiry_seconds(expires_at)
    if until_expiry is None:
        logger.warning(
            "[ingest] Duplicate-defer: lease expiry %r unparseable; using short "
            "%ds fallback instead of blocking a full cycle",
            expires_at,
            fallback_seconds,
        )
        base = fallback_seconds
    else:
        # Wake right after the current GCS owner lease is actually stealable so
        # the redelivered duplicate can acquire it. Pub/Sub caps
        # modify_ack_deadline at 600s, so a lease TTL above that self-corrects
        # within a couple of redeliveries rather than in one.
        base = max(0, int(until_expiry) + steal_grace + buffer_seconds)
    if base > 0 and jitter_seconds > 0:
        base += random.randint(0, jitter_seconds)
    return max(0, min(base, max_seconds))


async def main() -> None:
    import argparse

    from unify.common.pipeline.work_queue import RetryWorkItem

    from .ingest_worker import handle_ingest_message
    from .pipeline_events import record_worker_event
    from .worker_utils import (
        DuplicateLiveAttempt,
        LeaseExtender,
        build_selfhost_worker_infra,
        build_worker_infra,
        initialize_worker_environment,
        install_signal_handlers,
        is_shutdown_requested,
        shutdown_aware_sleep,
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
    # No boot-time `activate_unify_context` call: the worker is shared
    # across many assistants and has no identity of its own. Per-message
    # activation lives in ``ingest_worker._with_unify_key`` which
    # resolves the caller's ``UNIFY_KEY`` from Orchestra and activates
    # the right (user_id, assistant_id) context before the Unify SDK is
    # touched.
    _ = args.project  # kept for CLI compatibility; project read per-msg.

    # Self-host binds local artifacts and the emulated queue; the handler
    # below is the same either way, which is what makes the local stack a
    # rehearsal of the hosted one rather than a separate code path.
    infra = (
        build_selfhost_worker_infra()
        if os.environ.get("SELF_HOST", "").strip() not in ("", "0", "false")
        else build_worker_infra()
    )

    logger.info("Ingest worker started, polling for messages...")

    consecutive_empty = 0
    try:
        while not is_shutdown_requested():
            try:
                # Pre-pull shutdown check: belt-and-suspenders against the
                # race where SIGTERM arrives between the while-guard and the
                # receive call. If SIGTERM lands mid-message after this point,
                # the ingest hot loop surrenders at the next chunk boundary
                # (see ingest_worker._before_insert_chunk): it releases the GCS
                # attempt-lease and nacks via RetryWorkItem so a surviving pod
                # resumes from the durable checkpoint within the deployment's
                # terminationGracePeriod (600s), rather than relying on lease
                # TTL expiry.
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
                    lease_max_lifetime_s, lease_max_extensions = _lease_lifetime_cap()
                    lease_extender = LeaseExtender(
                        work_queue=infra.work_queue,
                        receipt_id=item.receipt_id,
                        job_id=job_id or None,
                        run_ledger=heartbeat_ledger,
                        run_id=job_id or None if heartbeat_ledger else None,
                        stage="ingest" if heartbeat_ledger else None,
                        max_lifetime_s=lease_max_lifetime_s,
                        max_extensions=lease_max_extensions,
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
                            should_surrender=lambda: lease_extender.surrendered,
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
                            os.environ.get("UNIFY_DUPLICATE_DEFER_MAX_ATTEMPTS", "12"),
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
