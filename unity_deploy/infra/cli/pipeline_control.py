#!/usr/bin/env python3
"""Operator CLI for GCP pipeline job control.

Commands:
    submit   -- Upload source files to GCS and publish ParseRequested (1 per file)
    list     -- List recent dispatches
    status   -- Show per-job status for a dispatch
    monitor  -- Read progress and run ledger from GCS for a single job
    cancel   -- Cancel a dispatch or single job
    pause    -- Pause a dispatch or single job (ack + park in-flight messages;
                workers deprovision via HPA when the backlog drains)
    resume   -- Resume a paused dispatch or job: re-publish parked messages in
                original publish order
    delete   -- Purge all GCS artifacts for a dispatch
    inspect  -- Read parse manifests and display table schemas/samples
    reconcile-dlq -- Persist Pub/Sub DLQ messages to GCS and mark jobs visible
    retry    -- Safely re-publish persisted DLQ/stale/error jobs
    verify   -- Compare declared manifest row_count vs durable checkpoint across
                a dispatch/job; non-zero exit on any shortfall (audit gate)

WARNING: Do not run ``retry`` and ``recover-stale`` on the same job
concurrently. Both publish an ingest message; two live messages for one job
contend for the GCS attempt-lease and can freeze the durable checkpoint,
leading to a silent under-ingest. Both commands now refuse to publish while a
recent in-flight message exists (override with ``--force`` only when you are
certain the prior message is gone).

Usage:
    python -m unity_deploy.infra.cli.pipeline_control submit \\
        --config path/to/pipeline_config.json --mode dm \\
        --user-id alice --assistant-id 42
    python -m unity_deploy.infra.cli.pipeline_control list
    python -m unity_deploy.infra.cli.pipeline_control status --dispatch-id <id>
    python -m unity_deploy.infra.cli.pipeline_control cancel --dispatch-id <id>
    python -m unity_deploy.infra.cli.pipeline_control cancel --job-id <id>
    python -m unity_deploy.infra.cli.pipeline_control pause --dispatch-id <id>
    python -m unity_deploy.infra.cli.pipeline_control pause --job-id <id>
    python -m unity_deploy.infra.cli.pipeline_control resume --dispatch-id <id>
    python -m unity_deploy.infra.cli.pipeline_control resume --job-id <id>
    python -m unity_deploy.infra.cli.pipeline_control delete --dispatch-id <id> --confirm
    python -m unity_deploy.infra.cli.pipeline_control monitor --job-id <id>
    python -m unity_deploy.infra.cli.pipeline_control inspect --job-id <id>
    python -m unity_deploy.infra.cli.pipeline_control reconcile-dlq --env staging --ack
    python -m unity_deploy.infra.cli.pipeline_control retry --env production \\
        --dispatch-id <id> --only dlq --dry-run
    python -m unity_deploy.infra.cli.pipeline_control retry --env production \\
        --dispatch-id <id> --only dlq --execute
    python -m unity_deploy.infra.cli.pipeline_control recover-stale --env production \\
        --dispatch-id <id> --dry-run
    python -m unity_deploy.infra.cli.pipeline_control verify --env production \\
        --dispatch-id <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from unity.common.pipeline._utils import utc_now_iso

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline_control",
        description="GCP pipeline job control CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- submit ------------------------------------------------------------

    p_submit = sub.add_parser(
        "submit",
        help="Submit a parse+ingest job (1 ParseRequested per file)",
    )
    p_submit.add_argument(
        "--config",
        required=True,
        help="Path to pipeline_config.json",
    )
    p_submit.add_argument(
        "--mode",
        required=True,
        choices=["fm", "dm"],
        help="Ingestion mode: fm (FileManager) or dm (DataManager)",
    )
    p_submit.add_argument("--user-id", required=True, help="User ID for bindings")
    p_submit.add_argument(
        "--assistant-id",
        required=True,
        help="Assistant ID for bindings",
    )
    p_submit.add_argument("--project", default="", help="Unify project name")
    p_submit.add_argument(
        "--alias",
        default="Local",
        help="FmBinding.fm_alias (FM mode only)",
    )
    p_submit.add_argument(
        "--target-context",
        default="",
        help="DmBinding.target_context (DM mode; derived from config tables if empty)",
    )
    p_submit.add_argument("--create-table-prefix", default="")
    p_submit.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Dispatch only the first N source files",
    )
    p_submit.add_argument("--debug", action="store_true")

    # -- list --------------------------------------------------------------

    p_list = sub.add_parser("list", help="List recent dispatches")
    p_list.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max dispatches to show (default: 20)",
    )
    p_list.add_argument("--debug", action="store_true")

    # -- status ------------------------------------------------------------

    p_status = sub.add_parser(
        "status",
        help="Show per-job status for a dispatch",
    )
    p_status.add_argument("--dispatch-id", required=True)
    p_status.add_argument("--env", default="", help="Pipeline environment override")
    p_status.add_argument("--project", default="", help="Pub/Sub project override")
    p_status.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    p_status.add_argument("--summary-only", action="store_true")
    p_status.add_argument("--show-events", action="store_true")
    p_status.add_argument("--show-checkpoints", action="store_true")
    p_status.add_argument("--show-dlq", action="store_true")
    p_status.add_argument("--show-retry-plan", action="store_true")
    p_status.add_argument("--debug", action="store_true")

    # -- monitor -----------------------------------------------------------

    p_monitor = sub.add_parser("monitor", help="Monitor a single running job")
    p_monitor.add_argument("--job-id", required=True)
    p_monitor.add_argument("--env", default="", help="Pipeline environment override")
    p_monitor.add_argument("--project", default="", help="Pub/Sub project override")
    p_monitor.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    p_monitor.add_argument("--show-events", action="store_true")
    p_monitor.add_argument("--show-checkpoints", action="store_true")
    p_monitor.add_argument("--show-dlq", action="store_true")
    p_monitor.add_argument("--show-retry-plan", action="store_true")
    p_monitor.add_argument(
        "--follow",
        action="store_true",
        help="Poll until completion",
    )
    p_monitor.add_argument("--interval", type=float, default=10.0)
    p_monitor.add_argument("--debug", action="store_true")

    # -- cancel ------------------------------------------------------------

    p_cancel = sub.add_parser("cancel", help="Cancel a dispatch or single job")
    cancel_target = p_cancel.add_mutually_exclusive_group(required=True)
    cancel_target.add_argument("--dispatch-id", default=None)
    cancel_target.add_argument("--job-id", default=None)
    p_cancel.add_argument("--reason", default="operator-initiated")
    p_cancel.add_argument("--debug", action="store_true")

    # -- pause -------------------------------------------------------------

    p_pause = sub.add_parser(
        "pause",
        help=(
            "Pause a dispatch or job. Sets status=paused in GCS, drains "
            "in-flight ingest messages into a GCS parking lot, and lets "
            "the HPA deprovision workers once the backlog hits zero."
        ),
    )
    pause_target = p_pause.add_mutually_exclusive_group(required=True)
    pause_target.add_argument("--dispatch-id", default=None)
    pause_target.add_argument("--job-id", default=None)
    p_pause.add_argument("--reason", default="operator-initiated")
    p_pause.add_argument(
        "--drain-batches",
        type=int,
        default=10,
        help=(
            "Upper bound on batches pulled from the ingest subscription "
            "when draining (each batch pulls up to 100 messages). "
            "Residual messages are parked by workers themselves when "
            "their control watcher detects the pause."
        ),
    )
    p_pause.add_argument("--debug", action="store_true")

    # -- resume ------------------------------------------------------------

    p_resume = sub.add_parser(
        "resume",
        help=(
            "Resume a paused dispatch or job. Re-publishes every parked "
            "message onto the ingest topic in original publish order, "
            "then flips status back to queued so the HPA can scale up."
        ),
    )
    resume_target = p_resume.add_mutually_exclusive_group(required=True)
    resume_target.add_argument("--dispatch-id", default=None)
    resume_target.add_argument("--job-id", default=None)
    p_resume.add_argument("--debug", action="store_true")

    # -- delete ------------------------------------------------------------

    p_delete = sub.add_parser(
        "delete",
        help="Purge all GCS artifacts for a dispatch",
    )
    p_delete.add_argument("--dispatch-id", required=True)
    p_delete.add_argument(
        "--confirm",
        action="store_true",
        help="Required flag to confirm deletion",
    )
    p_delete.add_argument("--debug", action="store_true")

    # -- inspect -----------------------------------------------------------

    p_inspect = sub.add_parser(
        "inspect",
        help="Inspect parse results for a job",
    )
    p_inspect.add_argument("--job-id", required=True)
    p_inspect.add_argument("--debug", action="store_true")

    # -- reconcile DLQ ------------------------------------------------------

    p_reconcile = sub.add_parser(
        "reconcile-dlq",
        help="Persist environment DLQ messages to GCS and mark affected jobs",
    )
    p_reconcile.add_argument("--env", default="", help="Pipeline environment override")
    p_reconcile.add_argument("--project", default="", help="Pub/Sub project override")
    p_reconcile.add_argument("--dispatch-id", default="")
    p_reconcile.add_argument("--job-id", default="")
    p_reconcile.add_argument("--limit", type=int, default=100)
    p_reconcile.add_argument(
        "--ack",
        action="store_true",
        help="Ack DLQ messages after durable GCS writes. Omit for dry-run.",
    )
    p_reconcile.add_argument("--json", action="store_true")
    p_reconcile.add_argument("--debug", action="store_true")

    # -- retry --------------------------------------------------------------

    p_retry = sub.add_parser(
        "retry",
        help="Safely republish failed, stale, or DLQ jobs from persisted state",
    )
    retry_target = p_retry.add_mutually_exclusive_group(required=True)
    retry_target.add_argument("--dispatch-id", default=None)
    retry_target.add_argument("--job-id", default=None)
    p_retry.add_argument("--env", default="", help="Pipeline environment override")
    p_retry.add_argument("--project", default="", help="Pub/Sub project override")
    p_retry.add_argument(
        "--only",
        action="append",
        choices=["dlq", "stale-running", "error", "retryable"],
        default=[],
    )
    p_retry.add_argument("--exclude-running-active", action="store_true", default=True)
    p_retry.add_argument("--max-jobs", type=int, default=0)
    p_retry.add_argument("--max-attempts", type=int, default=3)
    p_retry.add_argument("--force", action="store_true")
    p_retry.add_argument("--force-reingest", action="store_true")
    p_retry.add_argument("--dry-run", action="store_true")
    p_retry.add_argument(
        "--execute",
        action="store_true",
        help="Publish retry messages",
    )
    p_retry.add_argument("--json", action="store_true")
    p_retry.add_argument("--debug", action="store_true")

    # -- recover stale -------------------------------------------------------

    p_recover = sub.add_parser(
        "recover-stale",
        help=(
            "Recover running-stale jobs from persisted parse outbox payloads "
            "and GCS checkpoints"
        ),
    )
    recover_target = p_recover.add_mutually_exclusive_group(required=True)
    recover_target.add_argument("--dispatch-id", default=None)
    recover_target.add_argument("--job-id", default=None)
    p_recover.add_argument("--env", default="", help="Pipeline environment override")
    p_recover.add_argument("--project", default="", help="Pub/Sub project override")
    p_recover.add_argument("--max-jobs", type=int, default=0)
    p_recover.add_argument("--max-attempts", type=int, default=3)
    p_recover.add_argument("--force", action="store_true")
    p_recover.add_argument("--dry-run", action="store_true")
    p_recover.add_argument(
        "--execute",
        action="store_true",
        help="Finalize complete stale jobs and publish recovery messages",
    )
    p_recover.add_argument("--json", action="store_true")
    p_recover.add_argument("--debug", action="store_true")

    # -- reconcile stale -----------------------------------------------------

    p_reconcile_stale = sub.add_parser(
        "reconcile-stale",
        help="Scan recent dispatches and recover bounded running-stale jobs",
    )
    p_reconcile_stale.add_argument("--env", default="")
    p_reconcile_stale.add_argument("--project", default="")
    p_reconcile_stale.add_argument("--dispatch-id", default="")
    p_reconcile_stale.add_argument("--job-id", default="")
    p_reconcile_stale.add_argument("--dispatch-limit", type=int, default=20)
    p_reconcile_stale.add_argument("--max-jobs", type=int, default=10)
    p_reconcile_stale.add_argument("--max-attempts", type=int, default=3)
    p_reconcile_stale.add_argument("--force", action="store_true")
    p_reconcile_stale.add_argument("--dry-run", action="store_true")
    p_reconcile_stale.add_argument(
        "--execute",
        action="store_true",
        help="Execute bounded stale recovery. Omit for dry-run.",
    )
    p_reconcile_stale.add_argument("--json", action="store_true")
    p_reconcile_stale.add_argument("--debug", action="store_true")

    # -- worker refresh check -----------------------------------------------

    p_refresh_check = sub.add_parser(
        "worker-refresh-check",
        help="Return whether it is safe to restart pipeline worker pods",
    )
    p_refresh_check.add_argument("--env", default="")
    p_refresh_check.add_argument("--project", default="")
    p_refresh_check.add_argument("--dispatch-limit", type=int, default=20)
    p_refresh_check.add_argument("--force", action="store_true")
    p_refresh_check.add_argument("--allow-active", action="store_true")
    p_refresh_check.add_argument("--allow-queued", action="store_true")
    p_refresh_check.add_argument("--json", action="store_true")
    p_refresh_check.add_argument("--debug", action="store_true")

    # -- verify --------------------------------------------------------------

    p_verify = sub.add_parser(
        "verify",
        help=(
            "Verify ingest completeness: declared manifest row_count vs durable "
            "checkpoint rows_committed across a dispatch or job. Exits non-zero "
            "if any table is short or unverifiable."
        ),
    )
    verify_target = p_verify.add_mutually_exclusive_group(required=True)
    verify_target.add_argument("--dispatch-id", default=None)
    verify_target.add_argument("--job-id", default=None)
    p_verify.add_argument("--env", default="", help="Pipeline environment override")
    p_verify.add_argument("--project", default="", help="Pub/Sub project override")
    p_verify.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Treat tables with an unknown declared row_count as failures "
            "(default: only a checkpoint short of a known total fails)"
        ),
    )
    p_verify.add_argument("--json", action="store_true")
    p_verify.add_argument("--debug", action="store_true")

    # -- throughput ----------------------------------------------------------

    p_throughput = sub.add_parser(
        "throughput",
        help=(
            "Live ingest throughput from durable checkpoints: aggregate rows/s, "
            "per-job progress + ETA, and stall detection. Immune to worker log "
            "spam (reads GCS checkpoints, not logs)."
        ),
    )
    throughput_target = p_throughput.add_mutually_exclusive_group(required=True)
    throughput_target.add_argument("--dispatch-id", default=None)
    throughput_target.add_argument("--job-id", default=None)
    p_throughput.add_argument("--env", default="", help="Pipeline environment override")
    p_throughput.add_argument("--project", default="", help="Pub/Sub project override")
    p_throughput.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Seconds between checkpoint samples (default: 60)",
    )
    p_throughput.add_argument(
        "--iterations",
        type=int,
        default=0,
        help="Stop after N samples (0 = run until all jobs terminal)",
    )
    p_throughput.add_argument("--json", action="store_true")
    p_throughput.add_argument("--debug", action="store_true")

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_runtime_overrides(args: argparse.Namespace) -> None:
    env = getattr(args, "env", "") or ""
    project = getattr(args, "project", "") or ""
    if env:
        os.environ["UNITY_GCP_PIPELINE_ENVIRONMENT"] = env
    if project:
        os.environ["UNITY_PUBSUB_PROJECT_ID"] = project


def _init_infra(debug: bool = False):
    from unity_deploy.infra.workers.worker_utils import (
        build_worker_infra,
        initialize_worker_environment,
    )

    initialize_worker_environment(debug=debug)
    return build_worker_infra()


def _get_job_store(infra):
    from unity_deploy.infra.gcp.deployment_stores import GcsDeploymentJobStore

    job_store = infra.job_store
    assert isinstance(job_store, GcsDeploymentJobStore)
    return job_store


def _get_artifact_store(infra):
    from unity_deploy.infra.gcp.artifact_store import GcsArtifactStore

    artifact_store = infra.artifact_store
    assert isinstance(artifact_store, GcsArtifactStore)
    return artifact_store


def _queue_resources(settings) -> dict[str, str]:
    project = settings.pubsub.project_id
    suffix = settings.env_suffix()
    parse_topic = f"{settings.pubsub.parse_topic}{suffix}"
    ingest_topic = f"{settings.pubsub.ingest_topic}{suffix}"
    dlq_topic = f"{settings.pubsub.dead_letter_topic}{suffix}"
    parse_sub = f"{settings.pubsub.parse_subscription}{suffix}"
    ingest_sub = f"{settings.pubsub.ingest_subscription}{suffix}"
    dlq_sub = f"{settings.pubsub.dead_letter_subscription}{suffix}"
    return {
        "environment": settings.environment,
        "project_id": project,
        "artifact_bucket": settings.artifact_store.bucket,
        "artifact_prefix": settings.artifact_store.prefix,
        "parse_topic": f"projects/{project}/topics/{parse_topic}",
        "ingest_topic": f"projects/{project}/topics/{ingest_topic}",
        "dead_letter_topic": f"projects/{project}/topics/{dlq_topic}",
        "parse_subscription": f"projects/{project}/subscriptions/{parse_sub}",
        "ingest_subscription": f"projects/{project}/subscriptions/{ingest_sub}",
        "dead_letter_subscription": (f"projects/{project}/subscriptions/{dlq_sub}"),
    }


def _timestamp_age_seconds(value: str) -> int | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))


def _load_job_snapshot(infra, job_id: str, *, include_events: bool = True):
    from unity_deploy.infra.gcp.pipeline_observability import (
        JobObservabilitySnapshot,
        derive_status,
        latest_heartbeat_at,
        list_job_checkpoints,
        list_job_dlq_records,
        list_job_events,
        read_active_leases,
    )

    artifact_store = _get_artifact_store(infra)
    try:
        job = infra.job_store.read_job(job_id)
        durable_status = job.status
    except Exception:
        job = None
        durable_status = "unknown"

    checkpoints = list_job_checkpoints(artifact_store, job_id)
    dlq_records = list_job_dlq_records(artifact_store, job_id)
    events = list_job_events(artifact_store, job_id) if include_events else []
    heartbeat_at = latest_heartbeat_at(artifact_store, job_id)
    leases = read_active_leases(artifact_store, job_id)
    # Queued-age signal for queued-stale detection: prefer an explicit
    # metadata "queued_at" (set whenever a job is (re)enqueued) and fall back
    # to the job's created_at for jobs predating that metadata.
    queued_at = ""
    if job is not None:
        queued_at = str(
            (job.metadata or {}).get("queued_at") or job.created_at or "",
        )
    derived_status, retry_classification, retry_eligible = derive_status(
        durable_status=durable_status,
        dlq_records=dlq_records,
        checkpoints=checkpoints,
        heartbeat_at=heartbeat_at,
        leases=leases,
        queued_at=queued_at,
    )
    latest_checkpoint = None
    if checkpoints:
        latest_checkpoint = max(
            checkpoints.values(),
            key=lambda cp: cp.last_updated or "",
        )
    latest_event_at = events[-1].recorded_at if events else ""
    latest_dlq = dlq_records[-1] if dlq_records else None
    from unity_deploy.infra.gcp.pipeline_observability import is_fresh_lease

    fresh_lease = next((lease for lease in leases if is_fresh_lease(lease)), None)
    metadata = job.metadata if job is not None else {}
    source_file = str(metadata.get("source_file") or metadata.get("file_path") or "")
    target_context = str(metadata.get("target_context") or "")
    if latest_dlq and latest_dlq.payload:
        source_file = source_file or str(
            latest_dlq.payload.get("file_path")
            or next(iter(latest_dlq.payload.get("file_paths", [])), ""),
        )
        dm_binding = latest_dlq.payload.get("dm_binding") or {}
        if isinstance(dm_binding, dict):
            target_context = target_context or str(
                dm_binding.get("target_context") or "",
            )
    if derived_status in {"dlq", "partial-dlq"}:
        next_action = f"pipeline_control retry --job-id {job_id} --only dlq --dry-run"
        queue_location = "dlq"
    elif derived_status in {"running-stale", "queued-stale"}:
        next_action = f"pipeline_control recover-stale --job-id {job_id} --dry-run"
        queue_location = "none"
    elif durable_status == "queued":
        next_action = "wait for parse/ingest backlog or inspect queue metrics"
        queue_location = "parse/ingest"
    else:
        next_action = ""
        queue_location = "none"

    if fresh_lease:
        status_reason = (
            f"fresh lease owner={fresh_lease.owner_id} "
            f"expires={fresh_lease.expires_at}"
        )
    elif heartbeat_at:
        status_reason = f"last heartbeat age={_timestamp_age_seconds(heartbeat_at)}s"
    elif dlq_records:
        status_reason = f"latest DLQ classification={retry_classification}"
    elif derived_status == "running-stale":
        status_reason = "durable status is running with no fresh heartbeat or lease"
    elif derived_status == "queued-stale":
        status_reason = (
            "durable status is queued past the stale threshold with no fresh "
            "heartbeat, lease, or in-flight message (limbo)"
        )
    else:
        status_reason = derived_status

    retry_payload_source = "dlq" if latest_dlq and latest_dlq.payload else ""
    checkpoint_complete: bool | None = None
    recovery_action = ""
    if derived_status in {"running-stale", "queued-stale"}:
        payload, outbox_source = _read_parse_outbox_payload(artifact_store, job_id)
        if payload is not None:
            retry_payload_source = outbox_source
            _, checkpoint_complete = _table_checkpoint_plan(
                artifact_store=artifact_store,
                payload=payload,
                checkpoints=checkpoints,
            )
            recovery_action = (
                "finalize_success" if checkpoint_complete else "republish_ingest"
            )
        else:
            retry_payload_source = outbox_source
            recovery_action = "needs_operator"
    return JobObservabilitySnapshot(
        job_id=job_id,
        dispatch_id=job.dispatch_id if job is not None else "",
        durable_status=durable_status,
        derived_status=derived_status,
        source_file=source_file,
        target_context=target_context,
        last_event_at=latest_event_at,
        latest_heartbeat_at=heartbeat_at,
        latest_checkpoint_rows=(
            latest_checkpoint.rows_committed if latest_checkpoint else 0
        ),
        latest_checkpoint_chunks=(
            latest_checkpoint.chunks_committed if latest_checkpoint else 0
        ),
        latest_checkpoint_table=(
            latest_checkpoint.artifact_id if latest_checkpoint else ""
        ),
        active_lease_owner=fresh_lease.owner_id if fresh_lease else "",
        active_lease_expires_at=fresh_lease.expires_at if fresh_lease else "",
        queue_location=queue_location,
        delivery_attempt=latest_dlq.delivery_attempt if latest_dlq else None,
        retry_classification=retry_classification,
        retry_eligible=retry_eligible,
        next_action=next_action,
        status_reason=status_reason,
        latest_heartbeat_age_seconds=_timestamp_age_seconds(heartbeat_at),
        checkpoint_complete=checkpoint_complete,
        retry_payload_source=retry_payload_source,
        recovery_action=recovery_action,
        dlq_records=dlq_records,
        checkpoints=checkpoints,
        events=events,
    )


def _mark_job_dlq(infra, record, dlq_keys: list[str]) -> None:
    from unity.common.pipeline._utils import utc_now_iso

    job_store = _get_job_store(infra)
    try:
        job = job_store.read_job(record.job_id)
    except Exception:
        return
    if job.status not in {"success", "cancelled"}:
        job.status = "error"
        job.finished_at = job.finished_at or utc_now_iso()
        job.error = (
            f"Message moved to DLQ: topic={record.retry_topic} "
            f"attempts={record.delivery_attempt}"
        )
    job.metadata = {
        **(job.metadata or {}),
        "queue_state": "partial-dlq" if record.checkpoint_snapshot else "dlq",
        "derived_status": "partial-dlq" if record.checkpoint_snapshot else "dlq",
        "dlq_record_keys": dlq_keys,
        "dlq_message_id": record.dlq_message_id,
        "dlq_subscription": record.dlq_subscription,
        "dlq_recorded_at": record.recorded_at,
        "retry_classification": record.retry_classification,
        "previous_status": record.previous_job_status,
    }
    job_store.upsert_job(job)


async def _publish_retry(infra, *, topic: str, payload: dict) -> str:
    return await infra.work_queue.publish(topic=topic, payload=payload)


def _parse_outbox_key(job_id: str) -> str:
    return f"jobs/{job_id}/outbox/parse.json"


def _read_parse_outbox_payload(
    artifact_store: Any,
    job_id: str,
) -> tuple[dict | None, str]:
    """Return the persisted IngestRequested payload from parse outbox, if present."""
    try:
        outbox = artifact_store.get_json(_parse_outbox_key(job_id))
    except Exception:
        return None, "missing"
    payload = outbox.get("payload") if isinstance(outbox, dict) else None
    if isinstance(payload, dict):
        return payload, "parse_outbox"
    return None, "malformed"


def _table_checkpoint_plan(
    *,
    artifact_store: Any,
    payload: dict | None,
    checkpoints: dict,
) -> tuple[list[dict], bool]:
    """Compare ingest-manifest tables with durable checkpoints."""
    if not payload:
        return [], False
    manifest_key = str(payload.get("manifest_key") or "")
    if not manifest_key:
        return [], False
    try:
        from unity.common.pipeline import IngestPlan

        manifest_payload = artifact_store.get_json(manifest_key)
        plan = IngestPlan.model_validate(manifest_payload)
    except Exception:
        logger.debug("Could not load ingest manifest for stale recovery", exc_info=True)
        return [], False

    rows: list[dict] = []
    complete = bool(plan.tables_meta)
    for meta in plan.tables_meta:
        table_id = str(meta.table_id or meta.label or "")
        if not table_id:
            complete = False
            continue
        handle = (plan.table_inputs or {}).get(table_id)
        expected_rows = (
            meta.row_count
            if meta.row_count is not None
            else getattr(handle, "row_count", None)
        )
        checkpoint = checkpoints.get(table_id)
        rows_committed = int(getattr(checkpoint, "rows_committed", 0) or 0)
        chunks_committed = int(getattr(checkpoint, "chunks_committed", 0) or 0)
        table_complete = expected_rows is not None and rows_committed >= int(
            expected_rows,
        )
        complete = complete and table_complete
        rows.append(
            {
                "table_id": table_id,
                "expected_rows": expected_rows,
                "rows_committed": rows_committed,
                "chunks_committed": chunks_committed,
                "complete": table_complete,
            },
        )
    return rows, complete


def _verify_jobs(
    infra,
    job_ids: list[str],
    *,
    strict: bool = False,
) -> tuple[list[dict], bool]:
    """Compare each job's declared manifest row_count to its durable checkpoint.

    Returns ``(results, all_ok)``. A job fails when any table's
    ``rows_committed`` is short of the declared ``row_count`` (the exact
    fact_TelematicsTrips 166k/206,719 signature), when the manifest cannot be
    resolved, or - under ``strict`` - when a table has no declared total to
    verify against. Destination context counts are intentionally left
    out-of-band to keep this off the worker hot path.
    """
    from unity_deploy.infra.gcp.pipeline_observability import list_job_checkpoints

    artifact_store = _get_artifact_store(infra)
    job_store = _get_job_store(infra)
    results: list[dict] = []
    all_ok = True
    for job_id in job_ids:
        try:
            job = job_store.read_job(job_id)
            durable_status = str(getattr(job, "status", "") or "")
        except Exception:
            durable_status = "unknown"
        checkpoints = list_job_checkpoints(artifact_store, job_id)
        payload, payload_source = _read_parse_outbox_payload(artifact_store, job_id)
        tables, _complete = _table_checkpoint_plan(
            artifact_store=artifact_store,
            payload=payload,
            checkpoints=checkpoints,
        )
        if not tables:
            all_ok = False
            results.append(
                {
                    "job_id": job_id,
                    "durable_status": durable_status,
                    "ok": False,
                    "reason": f"unverifiable (no manifest; payload={payload_source})",
                    "tables": [],
                },
            )
            continue
        shorts: list[dict] = []
        unknown: list[str] = []
        for row in tables:
            expected = row["expected_rows"]
            if expected is None:
                unknown.append(row["table_id"])
            elif int(row["rows_committed"]) < int(expected):
                shorts.append(row)
        ok = not shorts and (not unknown or not strict)
        if not ok:
            all_ok = False
        reason = ""
        if shorts:
            reason = "short: " + "; ".join(
                f"{r['table_id']} {r['rows_committed']}/{r['expected_rows']}"
                for r in shorts
            )
        elif unknown:
            reason = "unknown declared row_count: " + ",".join(unknown)
        results.append(
            {
                "job_id": job_id,
                "durable_status": durable_status,
                "ok": ok,
                "reason": reason,
                "tables": tables,
            },
        )
    return results, all_ok


# Freshness window for the in-flight publish guard. A job queued by
# retry/recover-stale within this window is presumed to still have a live (or
# about-to-be-delivered) ingest message; publishing a second one creates the
# duplicate-message lease/checkpoint race that silently under-ingests (the
# fact_TelematicsTrips 166k/206,719 case). Markers older than the window are
# treated as a lost message so recovery can proceed without --force.
_INFLIGHT_GUARD_SECONDS = int(os.environ.get("UNITY_INFLIGHT_GUARD_SECONDS", "900"))


def _recent_inflight_publish(job: Any, *, now: datetime | None = None) -> str | None:
    """Return a skip reason if ``job`` has a recent in-flight publish from
    either ``retry`` or ``recover-stale``; otherwise ``None``.

    This is the cross-command guard: ``retry`` and ``recover-stale`` each only
    track their own publish marker, so without this a second command would
    re-publish while the first message is still in flight. Returns a reason
    while the marker is fresh (status queued/running); stale markers are
    ignored so a genuinely lost message can still be recovered.
    """
    metadata = getattr(job, "metadata", None) or {}
    status = str(getattr(job, "status", "") or "")
    if status not in {"queued", "running"}:
        return None
    retry_msg = metadata.get("last_retry_message_id")
    stale_msg = metadata.get("last_stale_recovery_message_id")
    msg_id = retry_msg or stale_msg
    if not msg_id:
        return None
    published_at = metadata.get("last_publish_at")
    if published_at:
        try:
            ts = datetime.fromisoformat(str(published_at))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now = now or datetime.now(timezone.utc)
            if (now - ts).total_seconds() > _INFLIGHT_GUARD_SECONDS:
                return None  # presumed lost; allow recovery without --force
        except Exception:
            pass  # unparseable timestamp -> conservatively treat as in-flight
    source = "retry" if retry_msg else "recover-stale"
    return (
        f"in-flight message {msg_id} from {source} (status={status}); "
        "use --force to publish anyway"
    )


def _plan_stale_recovery(
    infra,
    job_ids: list[str],
    *,
    max_jobs: int,
    max_attempts: int,
    force: bool,
) -> tuple[list[dict], list[dict]]:
    """Build a bounded stale recovery plan without mutating state."""
    artifact_store = _get_artifact_store(infra)
    job_store = _get_job_store(infra)
    plan: list[dict] = []
    skipped: list[dict] = []
    for job_id in job_ids:
        snap = _load_job_snapshot(infra, job_id, include_events=False)
        if snap.durable_status in {"success", "cancelled", "paused"} and not force:
            skipped.append({"job_id": job_id, "reason": snap.durable_status})
            continue
        if snap.derived_status == "running-active" and not force:
            skipped.append({"job_id": job_id, "reason": "fresh lease or heartbeat"})
            continue
        if snap.derived_status not in {"running-stale", "queued-stale"} and not force:
            skipped.append(
                {
                    "job_id": job_id,
                    "reason": f"not stale ({snap.derived_status})",
                },
            )
            continue
        if snap.retry_classification == "non_retryable" and not force:
            skipped.append({"job_id": job_id, "reason": "non-retryable classification"})
            continue
        try:
            job = job_store.read_job(job_id)
            recovery_attempt = (
                int((job.metadata or {}).get("stale_recovery_attempt", 0)) + 1
            )
            inflight_reason = _recent_inflight_publish(job)
            if inflight_reason and not force:
                skipped.append({"job_id": job_id, "reason": inflight_reason})
                continue
            if recovery_attempt > max_attempts and not force:
                skipped.append(
                    {
                        "job_id": job_id,
                        "reason": f"recovery budget exhausted ({recovery_attempt})",
                    },
                )
                continue
        except Exception:
            job = None
            recovery_attempt = 1

        payload, payload_source = _read_parse_outbox_payload(artifact_store, job_id)
        tables, checkpoints_complete = _table_checkpoint_plan(
            artifact_store=artifact_store,
            payload=payload,
            checkpoints=snap.checkpoints,
        )
        total_rows = sum(int(row["rows_committed"] or 0) for row in tables)
        if checkpoints_complete:
            action = "finalize_success"
        elif payload is not None:
            action = "republish_ingest"
        else:
            action = "needs_operator"
        plan.append(
            {
                "job_id": job_id,
                "dispatch_id": snap.dispatch_id,
                "durable_status": snap.durable_status,
                "derived_status": snap.derived_status,
                "payload_source": payload_source,
                "action": action,
                "checkpoint_complete": checkpoints_complete,
                "checkpoints": tables,
                "resume_rows": total_rows,
                "resume_chunks": sum(
                    int(row["chunks_committed"] or 0) for row in tables
                ),
                "payload": payload,
                "recovery_attempt": recovery_attempt,
            },
        )
        if max_jobs and len(plan) >= max_jobs:
            break
    return plan, skipped


def _select_stale_recovery_jobs(infra, args: argparse.Namespace) -> list[str]:
    job_id = getattr(args, "job_id", "") or ""
    if job_id:
        return [job_id]
    dispatch_id = getattr(args, "dispatch_id", "") or ""
    job_store = _get_job_store(infra)
    if dispatch_id:
        return list(job_store.read_dispatch(dispatch_id).job_ids)
    job_ids: list[str] = []
    seen: set[str] = set()
    for manifest in job_store.list_dispatches(
        limit=int(getattr(args, "dispatch_limit", 20)),
    ):
        for candidate in manifest.job_ids:
            if candidate not in seen:
                seen.add(candidate)
                job_ids.append(candidate)
    return job_ids


async def _execute_stale_recovery(
    infra,
    plan: list[dict],
) -> list[dict]:
    """Apply a stale recovery plan: finalize complete jobs or requeue payloads."""
    from unity_deploy.infra.gcp.pipeline_observability import (
        PipelineJobEvent,
        write_job_event,
    )

    artifact_store = _get_artifact_store(infra)
    job_store = _get_job_store(infra)
    applied: list[dict] = []
    for item in plan:
        job_id = item["job_id"]
        action = item["action"]
        base_event = PipelineJobEvent(
            event_type="stale_recovery_requested",
            environment=infra.settings.environment,
            project_id=infra.settings.pubsub.project_id,
            job_id=job_id,
            dispatch_id=item["dispatch_id"],
            stage="ingest",
            next_action=action,
            metadata={
                "payload_source": item["payload_source"],
                "recovery_attempt": item["recovery_attempt"],
                "resume_rows": item["resume_rows"],
                "resume_chunks": item["resume_chunks"],
                "checkpoint_complete": item["checkpoint_complete"],
            },
        )
        write_job_event(artifact_store, base_event)
        try:
            job = job_store.read_job(job_id)
            previous_status = job.status
            if action == "finalize_success":
                job.status = "success"
                job.finished_at = job.finished_at or utc_now_iso()
                job.error = None
                job.metadata = {
                    **(job.metadata or {}),
                    "stale_recovery_attempt": item["recovery_attempt"],
                    "stale_recovery_action": "finalize_success",
                    "stale_recovery_source": "pipeline_control recover-stale",
                    "previous_status": previous_status,
                    "total_rows_inserted": item["resume_rows"],
                }
                job_store.upsert_job(job)
                write_job_event(
                    artifact_store,
                    base_event.model_copy(
                        update={
                            "event_id": uuid4().hex,
                            "event_type": "stale_recovery_finalized_success",
                            "next_action": "terminal_success",
                            "recorded_at": utc_now_iso(),
                        },
                    ),
                )
                applied.append({"job_id": job_id, "action": action})
                continue
            if action == "republish_ingest":
                message_id = await _publish_retry(
                    infra,
                    topic="ingest",
                    payload=item["payload"],
                )
                job.status = "queued"
                job.finished_at = None
                job.error = None
                job.metadata = {
                    **(job.metadata or {}),
                    "stale_recovery_attempt": item["recovery_attempt"],
                    "stale_recovery_action": "republish_ingest",
                    "stale_recovery_source": "parse_outbox",
                    "previous_status": previous_status,
                    "last_stale_recovery_message_id": message_id,
                    "last_publish_at": utc_now_iso(),
                    "queued_at": utc_now_iso(),
                    "resume_rows": item["resume_rows"],
                    "resume_chunks": item["resume_chunks"],
                }
                job_store.upsert_job(job)
                write_job_event(
                    artifact_store,
                    base_event.model_copy(
                        update={
                            "event_id": uuid4().hex,
                            "event_type": "stale_recovery_republished",
                            "pubsub_message_id": message_id,
                            "next_action": "queued",
                            "recorded_at": utc_now_iso(),
                        },
                    ),
                )
                applied.append(
                    {"job_id": job_id, "action": action, "message_id": message_id},
                )
                continue
            job.error = (
                "running-stale recovery needs operator: missing parse outbox payload"
            )
            job.status = "error"
            job.finished_at = job.finished_at or utc_now_iso()
            job.metadata = {
                **(job.metadata or {}),
                "stale_recovery_attempt": item["recovery_attempt"],
                "stale_recovery_action": "needs_operator",
                "stale_recovery_source": item["payload_source"],
                "previous_status": previous_status,
            }
            job_store.upsert_job(job)
            write_job_event(
                artifact_store,
                base_event.model_copy(
                    update={
                        "event_id": uuid4().hex,
                        "event_type": "stale_recovery_needs_operator",
                        "next_action": "operator_review",
                        "recorded_at": utc_now_iso(),
                    },
                ),
            )
            applied.append({"job_id": job_id, "action": action})
        except Exception as exc:
            write_job_event(
                artifact_store,
                base_event.model_copy(
                    update={
                        "event_id": uuid4().hex,
                        "event_type": "stale_recovery_failed",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "next_action": "kept_prior_state",
                        "recorded_at": utc_now_iso(),
                    },
                ),
            )
            raise
    return applied


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def cmd_submit(args: argparse.Namespace) -> None:
    """Upload source files and publish one ParseRequested per file."""
    from unity.common.pipeline import DispatchTarget, publish_parse_request
    from unity.common.pipeline.deployment.types import (
        DeploymentBundleRef,
        DeploymentIngestionJob,
        DispatchManifest,
    )
    from unity.common.pipeline.types import DmBinding, FmBinding

    from unity_deploy.assistant_deployments.scripts.ingest_utils import (
        load_pipeline_config,
    )
    from unity_deploy.assistant_deployments.types.pipeline_config import (
        build_table_config_for_source_file,
    )

    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)

    config = load_pipeline_config(args.config)
    source_files = config.source_files
    if args.limit is not None and args.limit > 0:
        source_files = source_files[: args.limit]

    settings = infra.settings
    target = DispatchTarget(
        project_id=settings.pubsub.project_id,
        bucket_name=settings.artifact_store.bucket,
        env_suffix=settings.env_suffix(),
    )

    dispatch_id = uuid4().hex
    run_mode = "file_manager" if args.mode == "fm" else "data_manager"

    logger.info(
        "Dispatch %s: submitting %d file(s) [mode=%s]",
        dispatch_id,
        len(source_files),
        args.mode,
    )

    errors = 0
    job_ids: list[str] = []
    for sf in source_files:
        file_path = sf.file_path
        if not Path(file_path).exists():
            logger.warning("Source file not found, skipping: %s", file_path)
            errors += 1
            continue

        fm_binding = None
        dm_binding = None
        if args.mode == "fm":
            fm_binding = FmBinding(
                user_id=args.user_id,
                assistant_id=args.assistant_id,
                fm_alias=args.alias,
                logical_path=file_path,
            )
        else:
            dm_context = args.target_context
            if not dm_context and sf.tables:
                dm_context = sf.tables[0].context
            dm_binding = DmBinding(
                user_id=args.user_id,
                assistant_id=args.assistant_id,
                target_context=dm_context,
                create_table_prefix=args.create_table_prefix,
            )

        table_config = (
            build_table_config_for_source_file(config, sf) if sf.tables else None
        )

        try:
            result = publish_parse_request(
                target=target,
                logical_path=file_path,
                ingestion_mode=args.mode,
                fm_binding=fm_binding,
                dm_binding=dm_binding,
                dispatch_id=dispatch_id,
                table_config=table_config,
                source_local_path=file_path,
            )

            job = DeploymentIngestionJob(
                job_id=result.job_id,
                dispatch_id=dispatch_id,
                bundle_ref=DeploymentBundleRef(
                    bundle_id=result.job_id,
                    manifest_path="",
                ),
                run_mode=run_mode,
                execution_target="staging",
                status="queued",
            )
            job_store.upsert_job(job)
            job_ids.append(result.job_id)

            logger.info(
                "  dispatched %s -> job=%s message_id=%s",
                file_path,
                result.job_id,
                result.message_id,
            )
        except Exception:
            logger.exception("  dispatch failed for %s", file_path)
            errors += 1

    if job_ids:
        manifest = DispatchManifest(
            dispatch_id=dispatch_id,
            source="pipeline_control",
            mode=args.mode,
            config_path=args.config,
            job_ids=job_ids,
            total_files=len(source_files),
        )
        job_store.write_dispatch(manifest)

    print(
        f"\n=== Dispatch {dispatch_id} complete "
        f"({len(job_ids)} dispatched, {errors} errors) ===",
    )
    print("  List:    pipeline_control list")
    print(f"  Status:  pipeline_control status --dispatch-id {dispatch_id}")
    print(f"  Cancel:  pipeline_control cancel --dispatch-id {dispatch_id}")


async def cmd_list(args: argparse.Namespace) -> None:
    """List recent dispatches."""
    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)

    manifests = job_store.list_dispatches(limit=args.limit)
    if not manifests:
        print("No dispatches found.")
        return

    hdr = f"{'DISPATCH_ID':<34} {'CREATED':<20} {'SOURCE':<20} {'MODE':<5} {'FILES':>5}  STATUS"
    print(hdr)
    print("-" * len(hdr))

    for m in manifests:
        status_counts: dict[str, int] = {}
        for jid in m.job_ids:
            try:
                job = job_store.read_job(jid)
                status_counts[job.status] = status_counts.get(job.status, 0) + 1
            except Exception:
                status_counts["unknown"] = status_counts.get("unknown", 0) + 1

        status_parts = []
        for s in (
            "success",
            "running",
            "queued",
            "paused",
            "error",
            "cancelled",
            "unknown",
        ):
            cnt = status_counts.get(s, 0)
            if cnt:
                status_parts.append(f"{cnt} {s}")
        status_str = ", ".join(status_parts) or "no jobs"

        created = m.created_at[:19].replace("T", " ") if m.created_at else "?"
        print(
            f"{m.dispatch_id:<34} {created:<20} {m.source:<20} {m.mode:<5} "
            f"{m.total_files:>5}  {status_str}",
        )


async def cmd_status(args: argparse.Namespace) -> None:
    """Show truthful per-job status for a dispatch."""
    _apply_runtime_overrides(args)
    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)

    try:
        manifest = job_store.read_dispatch(args.dispatch_id)
    except Exception:
        print(f"Dispatch {args.dispatch_id} not found")
        return

    resources = _queue_resources(infra.settings)
    created = manifest.created_at[:19].replace("T", " ") if manifest.created_at else "?"
    if not args.json:
        print(f"\nDispatch: {manifest.dispatch_id}", flush=True)
        print(f"Source:   {manifest.source}", flush=True)
        print(f"Mode:     {manifest.mode}", flush=True)
        print(f"Created:  {created}", flush=True)
        print(f"Config:   {manifest.config_path or '(ad-hoc)'}", flush=True)
        print(f"Files:    {manifest.total_files}", flush=True)
        print("\nEnvironment / Resources:", flush=True)
        for key, value in resources.items():
            print(f"  {key}: {value or '(unset)'}", flush=True)
        print(
            f"\nScanning {len(manifest.job_ids)} job(s) for status...",
            flush=True,
        )
    snapshots = []
    for idx, jid in enumerate(manifest.job_ids, start=1):
        if not args.json:
            print(f"  [{idx}/{len(manifest.job_ids)}] inspecting {jid}", flush=True)
        snapshots.append(
            _load_job_snapshot(infra, jid, include_events=args.show_events),
        )
    counts: dict[str, int] = {}
    for snap in snapshots:
        counts[snap.derived_status] = counts.get(snap.derived_status, 0) + 1

    if args.json:
        print(
            json.dumps(
                {
                    "dispatch": manifest.model_dump(mode="json"),
                    "resources": resources,
                    "counts": counts,
                    "jobs": [snap.model_dump(mode="json") for snap in snapshots],
                },
                indent=2,
            ),
        )
        return

    print()

    hdr = (
        f"  {'JOB_ID':<34} {'JOB_STATUS':<11} {'DERIVED':<15} "
        f"{'QUEUE':<10} {'ROWS':>10} {'CHUNKS':>7} {'DLQ_ATT':>7} ACTION"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for snap in snapshots:
        action = snap.next_action if args.show_retry_plan else ""
        print(
            f"  {snap.job_id:<34} {snap.durable_status:<11} "
            f"{snap.derived_status:<15} {snap.queue_location:<10} "
            f"{snap.latest_checkpoint_rows:>10} {snap.latest_checkpoint_chunks:>7} "
            f"{str(snap.delivery_attempt or ''):>7} {action}",
        )
        if args.show_retry_plan and (
            snap.status_reason or snap.retry_payload_source or snap.recovery_action
        ):
            print(
                f"    reason={snap.status_reason or '-'} "
                f"payload_source={snap.retry_payload_source or '-'} "
                f"recovery_action={snap.recovery_action or '-'}",
            )

    print()
    ordered_counts = ", ".join(
        f"{count} {status}" for status, count in sorted(counts.items())
    )
    print(f"  Summary: {ordered_counts or 'no jobs'}")

    retryable = [snap for snap in snapshots if snap.retry_eligible]
    non_retryable_dlq = [
        snap
        for snap in snapshots
        if snap.dlq_records and snap.retry_classification == "non_retryable"
    ]
    if retryable:
        print("\nRetryable / Operator-Retryable Jobs:")
        for snap in retryable:
            print(
                f"  {snap.job_id}: {snap.derived_status} "
                f"class={snap.retry_classification} cmd={snap.next_action}",
            )
    if non_retryable_dlq:
        print("\nNon-Retryable DLQ Jobs:")
        for snap in non_retryable_dlq:
            latest = snap.dlq_records[-1]
            print(
                f"  {snap.job_id}: class={latest.retry_classification} "
                f"error={latest.error or '(native DLQ; inspect record)'}",
            )
    if args.summary_only:
        return

    if args.show_dlq:
        print("\nDLQ Records:")
        for snap in snapshots:
            for record in snap.dlq_records[-3:]:
                print(
                    f"  {snap.job_id}: msg={record.dlq_message_id} "
                    f"topic={record.retry_topic} attempts={record.delivery_attempt} "
                    f"class={record.retry_classification} at={record.recorded_at}",
                )
    if args.show_checkpoints:
        print("\nCheckpoint Progress:")
        for snap in snapshots:
            for artifact_id, checkpoint in snap.checkpoints.items():
                print(
                    f"  {snap.job_id}/{artifact_id}: "
                    f"rows={checkpoint.rows_committed} "
                    f"chunks={checkpoint.chunks_committed} "
                    f"updated={checkpoint.last_updated}",
                )
    if args.show_events:
        print("\nLatest Events:")
        for snap in snapshots:
            for event in snap.events[-5:]:
                print(
                    f"  {snap.job_id}: {event.recorded_at} "
                    f"{event.event_type} stage={event.stage} action={event.next_action} "
                    f"err={event.error_message}",
                )


async def cmd_monitor(args: argparse.Namespace) -> None:
    """Read progress and run ledger from GCS for a single job."""
    _apply_runtime_overrides(args)
    infra = _init_infra(debug=args.debug)
    settings = infra.settings

    while True:
        try:
            job = infra.job_store.read_job(args.job_id)
        except FileNotFoundError:
            print(f"Job {args.job_id} not found")
            return

        if not args.json:
            print(f"\nLoading snapshot for job {args.job_id}...", flush=True)
            print("Monitor resources:", flush=True)
            for key, value in _queue_resources(settings).items():
                print(f"  {key}: {value or '(unset)'}", flush=True)
        snap = _load_job_snapshot(infra, args.job_id, include_events=args.show_events)
        if args.json:
            print(
                json.dumps(
                    {
                        "resources": _queue_resources(settings),
                        "job": job.model_dump(mode="json"),
                        "snapshot": snap.model_dump(mode="json"),
                    },
                    indent=2,
                ),
            )
            if not args.follow or job.status in ("success", "error", "cancelled"):
                break
            await asyncio.sleep(args.interval)
            continue

        print(f"\n{'='*60}")
        print(f"Job: {job.job_id}")
        print(f"Durable Status: {job.status}")
        print(f"Derived Status: {snap.derived_status}")
        print(f"Queue Location: {snap.queue_location}")
        print(f"Retry Eligibility: {snap.retry_eligible} ({snap.retry_classification})")
        if snap.next_action:
            print(f"Recommended Action: {snap.next_action}")
        if snap.status_reason:
            print(f"Status Reason: {snap.status_reason}")
        if snap.retry_payload_source or snap.recovery_action:
            print(
                "Recovery: "
                f"payload_source={snap.retry_payload_source or '-'} "
                f"checkpoint_complete={snap.checkpoint_complete} "
                f"action={snap.recovery_action or '-'}",
            )
        if job.dispatch_id:
            print(f"Dispatch: {job.dispatch_id}")
        if job.started_at:
            print(f"Started: {job.started_at}")
        if job.finished_at:
            print(f"Finished: {job.finished_at}")
        if job.cancelled_at:
            print(f"Cancelled: {job.cancelled_at} ({job.cancel_reason})")
        if job.error:
            print(f"Error: {job.error}")
        if job.metadata:
            print(f"Metadata: {json.dumps(job.metadata, indent=2)}")
        if snap.latest_heartbeat_at:
            print(f"Latest heartbeat: {snap.latest_heartbeat_at}")
        if snap.active_lease_owner:
            print(
                f"Active lease: owner={snap.active_lease_owner} "
                f"expires={snap.active_lease_expires_at}",
            )
        if snap.latest_checkpoint_table:
            print(
                f"Latest checkpoint: table={snap.latest_checkpoint_table} "
                f"rows={snap.latest_checkpoint_rows} "
                f"chunks={snap.latest_checkpoint_chunks}",
            )
        if snap.derived_status in {"dlq", "partial-dlq"}:
            print(
                "WARNING: job is not actively running; a persisted DLQ record "
                "exists for its queue message.",
            )

        prefix = settings.artifact_store.prefix.strip("/")
        job_root = f"{prefix}/jobs/{args.job_id}" if prefix else f"jobs/{args.job_id}"
        ledger_key = f"{job_root}/run_ledger.jsonl"
        try:
            bucket = infra.storage_client.bucket(settings.artifact_store.bucket)
            blob = bucket.blob(ledger_key)
            if blob.exists():
                content = blob.download_as_text(encoding="utf-8")
                lines = [line for line in content.strip().split("\n") if line.strip()]
                print(f"\nRun ledger ({len(lines)} entries):")
                for line in lines[-10:]:
                    entry = json.loads(line)
                    status = entry.get("status", "?")
                    stage = entry.get("stage_name", "?")
                    fp = entry.get("file_path", "?")
                    dur = entry.get("duration_ms", 0)
                    print(f"  [{status}] {stage}: {fp} ({dur:.0f}ms)")
        except Exception:
            pass

        if args.show_dlq or snap.dlq_records:
            print(f"\nDLQ records ({len(snap.dlq_records)}):")
            for record in snap.dlq_records[-10:]:
                print(
                    f"  msg={record.dlq_message_id} topic={record.retry_topic} "
                    f"attempts={record.delivery_attempt} "
                    f"class={record.retry_classification} at={record.recorded_at}",
                )
                if record.error:
                    print(f"    error={record.error}")
        if args.show_checkpoints:
            print(f"\nCheckpoints ({len(snap.checkpoints)}):")
            for artifact_id, checkpoint in snap.checkpoints.items():
                print(
                    f"  {artifact_id}: rows={checkpoint.rows_committed} "
                    f"chunks={checkpoint.chunks_committed} "
                    f"updated={checkpoint.last_updated}",
                )
        if args.show_events:
            print(f"\nEvents ({len(snap.events)}):")
            for event in snap.events[-20:]:
                print(
                    f"  {event.recorded_at} {event.event_type} stage={event.stage} "
                    f"action={event.next_action} err={event.error_message}",
                )

        if not args.follow or job.status in ("success", "error", "cancelled"):
            break

        print(f"\nPolling in {args.interval}s...")
        await asyncio.sleep(args.interval)


async def cmd_reconcile_dlq(args: argparse.Namespace) -> None:
    """Persist DLQ messages to GCS and update job status metadata."""
    from unity_deploy.infra.gcp.pipeline_observability import (
        PipelineJobEvent,
        checkpoint_snapshot,
        dlq_record_from_received_item,
        extract_queue_identity,
        list_job_checkpoints,
        write_dlq_record,
        write_job_event,
    )

    _apply_runtime_overrides(args)
    infra = _init_infra(debug=args.debug)
    artifact_store = _get_artifact_store(infra)
    resources = _queue_resources(infra.settings)
    items = await infra.work_queue.receive(
        max_messages=max(1, args.limit),
        topics=["dead_letter"],
    )
    results = []
    for item in items:
        payload = item.payload or {}
        identity = extract_queue_identity(payload)
        job_id = identity.job_id
        dispatch_id = identity.dispatch_id
        if args.job_id and job_id != args.job_id:
            await infra.work_queue.retry(
                item.receipt_id,
                error="dlq-reconcile filter mismatch",
                delay_seconds=0,
            )
            continue
        if args.dispatch_id and dispatch_id != args.dispatch_id:
            await infra.work_queue.retry(
                item.receipt_id,
                error="dlq-reconcile filter mismatch",
                delay_seconds=0,
            )
            continue
        try:
            previous_status = ""
            job = None
            try:
                job = infra.job_store.read_job(job_id) if job_id else None
                previous_status = job.status if job is not None else ""
                if not dispatch_id and job is not None:
                    dispatch_id = str(job.dispatch_id or "")
            except Exception:
                pass
            checkpoints = list_job_checkpoints(artifact_store, job_id) if job_id else {}
            record = dlq_record_from_received_item(
                item,
                environment=infra.settings.environment,
                project_id=infra.settings.pubsub.project_id,
                dlq_subscription=resources["dead_letter_subscription"],
                checkpoint_snapshot=checkpoint_snapshot(checkpoints),
                previous_job_status=previous_status,
            )
            if not record.job_id:
                record.job_id = job_id or "unknown"
            if not record.dispatch_id:
                record.dispatch_id = dispatch_id
            dlq_keys = [] if not args.ack else write_dlq_record(artifact_store, record)
            event = PipelineJobEvent(
                event_type="native_dlq_reconciled",
                environment=infra.settings.environment,
                project_id=infra.settings.pubsub.project_id,
                job_id=record.job_id,
                dispatch_id=record.dispatch_id,
                stage=record.retry_topic,
                pubsub_message_id=record.dlq_message_id,
                delivery_attempt=record.delivery_attempt,
                source_subscription=record.source_subscription,
                retry_classification=record.retry_classification,
                next_action="acked_dlq" if args.ack else "dry_run",
                metadata={"dlq_record_keys": dlq_keys},
            )
            if args.ack:
                write_job_event(artifact_store, event)
                _mark_job_dlq(infra, record, dlq_keys)
                await infra.work_queue.ack(item.receipt_id)
            else:
                await infra.work_queue.retry(
                    item.receipt_id,
                    error="dlq-reconcile dry-run",
                    delay_seconds=0,
                )
            results.append(
                {
                    "job_id": record.job_id,
                    "dispatch_id": record.dispatch_id,
                    "topic": record.retry_topic,
                    "delivery_attempt": record.delivery_attempt,
                    "classification": record.retry_classification,
                    "acked": bool(args.ack),
                    "keys": dlq_keys,
                },
            )
        except Exception as exc:
            await infra.work_queue.retry(
                item.receipt_id,
                error=f"dlq-reconcile failed before durable write: {exc}",
                delay_seconds=0,
            )
            raise

    if args.json:
        print(json.dumps({"resources": resources, "results": results}, indent=2))
        return
    print("DLQ reconciliation resources:")
    for key, value in resources.items():
        print(f"  {key}: {value or '(unset)'}")
    mode = "ACKING after durable write" if args.ack else "DRY-RUN only; no DLQ acks"
    print(f"\nMode: {mode}")
    print(f"Pulled {len(items)} DLQ message(s), matched {len(results)}.")
    for row in results:
        print(
            f"  job={row['job_id']} dispatch={row['dispatch_id']} "
            f"topic={row['topic']} attempts={row['delivery_attempt']} "
            f"class={row['classification']} acked={row['acked']}",
        )


async def cmd_retry(args: argparse.Namespace) -> None:
    """Safely retry DLQ/stale/error jobs from persisted payloads."""
    from unity_deploy.infra.gcp.pipeline_observability import (
        PipelineJobEvent,
        write_job_event,
    )

    _apply_runtime_overrides(args)
    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)
    artifact_store = _get_artifact_store(infra)
    resources = _queue_resources(infra.settings)
    execute = bool(args.execute and not args.dry_run)
    target_jobs: list[str] = []
    if args.job_id:
        target_jobs = [args.job_id]
    else:
        manifest = job_store.read_dispatch(args.dispatch_id)
        target_jobs = list(manifest.job_ids)

    only = set(args.only or [])
    if not only:
        only = {"dlq", "stale-running", "error", "retryable"}

    if not args.job_id and only == {"dlq"}:
        from unity_deploy.infra.gcp.pipeline_observability import (
            list_dispatch_dlq_records,
        )

        dlq_job_ids: list[str] = []
        seen_dlq_jobs: set[str] = set()
        manifest_jobs = set(target_jobs)
        for record in list_dispatch_dlq_records(artifact_store, args.dispatch_id):
            if record.job_id in manifest_jobs and record.job_id not in seen_dlq_jobs:
                seen_dlq_jobs.add(record.job_id)
                dlq_job_ids.append(record.job_id)
        if dlq_job_ids:
            target_jobs = dlq_job_ids

    if not args.json:
        print("Retry resources:", flush=True)
        for key, value in resources.items():
            print(f"  {key}: {value or '(unset)'}", flush=True)
        print(
            f"\nPlanning retry for {len(target_jobs)} job(s) "
            f"[filters={','.join(sorted(only))}]...",
            flush=True,
        )

    plan: list[dict] = []
    skipped: list[dict] = []
    for idx, job_id in enumerate(target_jobs, start=1):
        if not args.json:
            print(f"  [{idx}/{len(target_jobs)}] inspecting {job_id}", flush=True)
        snap = _load_job_snapshot(infra, job_id, include_events=False)
        reasons: list[str] = []
        if snap.durable_status == "success" and not args.force_reingest:
            skipped.append({"job_id": job_id, "reason": "success"})
            continue
        if snap.derived_status == "running-active" and not args.force:
            skipped.append({"job_id": job_id, "reason": "fresh lease or heartbeat"})
            continue
        if snap.dlq_records and "dlq" in only:
            reasons.append("dlq")
        if snap.derived_status == "running-stale" and "stale-running" in only:
            reasons.append("stale-running")
        if snap.durable_status == "error" and "error" in only:
            reasons.append("error")
        if snap.retry_eligible and "retryable" in only:
            reasons.append("retryable")
        if not reasons:
            skipped.append(
                {"job_id": job_id, "reason": f"filtered ({snap.derived_status})"},
            )
            continue
        if snap.retry_classification == "non_retryable" and not args.force:
            skipped.append({"job_id": job_id, "reason": "non-retryable classification"})
            continue
        retry_attempt = 1
        try:
            job = job_store.read_job(job_id)
            retry_attempt = int((job.metadata or {}).get("retry_attempt", 0)) + 1
            inflight_reason = _recent_inflight_publish(job)
            if inflight_reason and not args.force:
                skipped.append({"job_id": job_id, "reason": inflight_reason})
                continue
            if retry_attempt > args.max_attempts and not args.force:
                skipped.append(
                    {
                        "job_id": job_id,
                        "reason": f"retry budget exhausted ({retry_attempt})",
                    },
                )
                continue
        except Exception:
            job = None
        if not snap.dlq_records:
            skipped.append({"job_id": job_id, "reason": "no persisted DLQ payload"})
            continue
        record = snap.dlq_records[-1]
        if record.retry_topic not in {"parse", "ingest"}:
            skipped.append({"job_id": job_id, "reason": "unknown retry topic"})
            continue
        plan.append(
            {
                "job_id": job_id,
                "dispatch_id": snap.dispatch_id,
                "topic": record.retry_topic,
                "payload": record.payload,
                "dlq_message_id": record.dlq_message_id,
                "checkpoint_rows": snap.latest_checkpoint_rows,
                "checkpoint_chunks": snap.latest_checkpoint_chunks,
                "retry_attempt": retry_attempt,
                "reasons": sorted(set(reasons)),
            },
        )
        if args.max_jobs and len(plan) >= args.max_jobs:
            break

    published: list[dict] = []
    if not args.json:
        print(f"\nMode: {'EXECUTE' if execute else 'DRY-RUN'}", flush=True)
        print(f"Would retry {len(plan)} job(s); skipped {len(skipped)}.", flush=True)
        for item in plan:
            print(
                f"  RETRY job={item['job_id']} topic={item['topic']} "
                f"checkpoint_rows={item['checkpoint_rows']} "
                f"attempt={item['retry_attempt']} reasons={','.join(item['reasons'])}",
                flush=True,
            )
        for item in skipped[:20]:
            print(
                f"  SKIP job={item['job_id']} reason={item['reason']}",
                flush=True,
            )
        if execute and plan:
            print(f"\nPublishing {len(plan)} retry message(s)...", flush=True)

    if execute:
        for item in plan:
            event = PipelineJobEvent(
                event_type="retry_publish_requested",
                environment=infra.settings.environment,
                project_id=infra.settings.pubsub.project_id,
                job_id=item["job_id"],
                dispatch_id=item["dispatch_id"],
                stage=item["topic"],
                next_action="publish_retry",
                metadata={
                    "dlq_message_id": item["dlq_message_id"],
                    "retry_attempt": item["retry_attempt"],
                    "reasons": item["reasons"],
                },
            )
            write_job_event(artifact_store, event)
            try:
                message_id = await _publish_retry(
                    infra,
                    topic=item["topic"],
                    payload=item["payload"],
                )
                if job_store:
                    job = job_store.read_job(item["job_id"])
                    previous_status = job.status
                    job.status = "queued"
                    job.finished_at = None
                    job.error = None
                    job.metadata = {
                        **(job.metadata or {}),
                        "retry_attempt": item["retry_attempt"],
                        "retry_reason": ",".join(item["reasons"]),
                        "retry_source": "pipeline_control retry",
                        "previous_status": previous_status,
                        "last_retry_message_id": message_id,
                        "last_publish_at": utc_now_iso(),
                    }
                    job_store.upsert_job(job)
                write_job_event(
                    artifact_store,
                    event.model_copy(
                        update={
                            "event_id": uuid4().hex,
                            "event_type": "retry_published",
                            "pubsub_message_id": message_id,
                            "next_action": "queued",
                            "recorded_at": utc_now_iso(),
                        },
                    ),
                )
                published.append({"job_id": item["job_id"], "message_id": message_id})
            except Exception as exc:
                write_job_event(
                    artifact_store,
                    event.model_copy(
                        update={
                            "event_id": uuid4().hex,
                            "event_type": "retry_publish_failed",
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "next_action": "kept_prior_state",
                            "recorded_at": utc_now_iso(),
                        },
                    ),
                )
                raise

    result = {
        "resources": resources,
        "dry_run": not execute,
        "plan": plan,
        "skipped": skipped,
        "published": published,
    }
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    if execute:
        print(f"\nPublished {len(published)} retry message(s).", flush=True)
        for item in published:
            print(
                f"  PUBLISHED job={item['job_id']} message_id={item['message_id']}",
                flush=True,
            )


async def cmd_recover_stale(args: argparse.Namespace) -> None:
    """Recover explicit running-stale jobs from parse outbox/checkpoints."""
    _apply_runtime_overrides(args)
    infra = _init_infra(debug=args.debug)
    resources = _queue_resources(infra.settings)
    execute = bool(args.execute and not args.dry_run)
    target_jobs = _select_stale_recovery_jobs(infra, args)
    plan, skipped = _plan_stale_recovery(
        infra,
        target_jobs,
        max_jobs=args.max_jobs,
        max_attempts=args.max_attempts,
        force=args.force,
    )
    applied: list[dict] = []
    if execute and plan:
        applied = await _execute_stale_recovery(infra, plan)

    result = {
        "resources": resources,
        "dry_run": not execute,
        "plan": plan,
        "skipped": skipped,
        "applied": applied,
    }
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return

    print("Stale recovery resources:", flush=True)
    for key, value in resources.items():
        print(f"  {key}: {value or '(unset)'}", flush=True)
    print(f"\nMode: {'EXECUTE' if execute else 'DRY-RUN'}", flush=True)
    print(f"Planned {len(plan)} job(s); skipped {len(skipped)}.", flush=True)
    for item in plan:
        print(
            f"  {item['action'].upper()} job={item['job_id']} "
            f"payload_source={item['payload_source']} "
            f"checkpoint_complete={item['checkpoint_complete']} "
            f"resume_rows={item['resume_rows']} "
            f"resume_chunks={item['resume_chunks']} "
            f"attempt={item['recovery_attempt']}",
            flush=True,
        )
        for row in item["checkpoints"]:
            print(
                f"    table={row['table_id']} rows={row['rows_committed']}/"
                f"{row['expected_rows']} chunks={row['chunks_committed']} "
                f"complete={row['complete']}",
                flush=True,
            )
    for item in skipped[:20]:
        print(f"  SKIP job={item['job_id']} reason={item['reason']}", flush=True)
    if execute:
        print(f"\nApplied {len(applied)} stale recovery action(s).", flush=True)
        for item in applied:
            suffix = f" message_id={item['message_id']}" if "message_id" in item else ""
            print(
                f"  APPLIED job={item['job_id']} action={item['action']}{suffix}",
                flush=True,
            )


async def cmd_reconcile_stale(args: argparse.Namespace) -> None:
    """Cron-friendly bounded stale-running reconciler."""
    await cmd_recover_stale(args)


async def cmd_verify(args: argparse.Namespace) -> None:
    """Verify ingest completeness (declared row_count vs durable checkpoint)."""
    _apply_runtime_overrides(args)
    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)
    if args.job_id:
        job_ids = [args.job_id]
    else:
        job_ids = list(job_store.read_dispatch(args.dispatch_id).job_ids)

    results, all_ok = _verify_jobs(infra, job_ids, strict=args.strict)

    if args.json:
        print(json.dumps({"ok": all_ok, "results": results}, indent=2, default=str))
        if not all_ok:
            sys.exit(1)
        return

    print(f"Verifying {len(job_ids)} job(s)...", flush=True)
    for result in results:
        mark = "OK  " if result["ok"] else "FAIL"
        suffix = f" {result['reason']}" if result["reason"] else ""
        print(
            f"  [{mark}] job={result['job_id']} "
            f"status={result['durable_status']}{suffix}",
            flush=True,
        )
        for row in result["tables"]:
            print(
                f"        table={row['table_id']} "
                f"rows={row['rows_committed']}/{row['expected_rows']} "
                f"complete={row['complete']}",
                flush=True,
            )
    ok_count = sum(1 for result in results if result["ok"])
    print(f"\n{ok_count}/{len(results)} job(s) verified complete.", flush=True)
    if not all_ok:
        print(
            "DISCREPANCY: at least one job is short of its declared row_count.",
            flush=True,
        )
        sys.exit(1)


_THROUGHPUT_TERMINAL_STATUSES = {"success", "error", "cancelled"}


def _collect_throughput_sample(infra, job_ids: list[str]) -> list[dict]:
    """Per-job committed/expected/status snapshot for one throughput tick.

    Reads durable GCS checkpoints (hardened: each blob is parsed and validated
    individually by ``list_job_checkpoints``), so it is immune to worker log
    spam and partial-read swings.
    """
    from unity_deploy.infra.gcp.pipeline_observability import list_job_checkpoints

    artifact_store = _get_artifact_store(infra)
    job_store = _get_job_store(infra)
    sample: list[dict] = []
    for job_id in job_ids:
        try:
            checkpoints = list_job_checkpoints(artifact_store, job_id)
        except Exception:
            checkpoints = {}
        committed = sum(int(cp.rows_committed or 0) for cp in checkpoints.values())
        try:
            job = job_store.read_job(job_id)
            status = job.status
            expected = int((job.metadata or {}).get("rows_expected") or 0)
        except Exception:
            status = "unknown"
            expected = 0
        sample.append(
            {
                "job_id": job_id,
                "committed_rows": committed,
                "expected_rows": expected,
                "status": status,
            },
        )
    return sample


def _build_throughput_report(
    *,
    current: list[dict],
    prev_by_job: dict[str, int] | None,
    dt_seconds: float,
) -> dict:
    """Compute aggregate rows/s, per-job deltas/ETA, and stall/terminal flags.

    Pure function (no I/O) so the rate math is unit-testable. ``prev_by_job`` is
    ``None`` on the first (baseline) sample, in which case rates are unknown.
    """
    total = sum(int(j["committed_rows"]) for j in current)
    per_job: list[dict] = []
    nonterminal = 0
    for j in current:
        terminal = j["status"] in _THROUGHPUT_TERMINAL_STATUSES
        if not terminal:
            nonterminal += 1
        prev = prev_by_job.get(j["job_id"]) if prev_by_job is not None else None
        delta = (j["committed_rows"] - prev) if prev is not None else None
        rate = (delta / dt_seconds) if (delta is not None and dt_seconds > 0) else None
        expected = int(j["expected_rows"] or 0)
        remaining = max(0, expected - j["committed_rows"]) if expected else None
        eta_seconds = (remaining / rate) if (remaining and rate and rate > 0) else None
        per_job.append(
            {
                "job_id": j["job_id"],
                "status": j["status"],
                "committed_rows": j["committed_rows"],
                "expected_rows": expected,
                "delta_rows": delta,
                "rows_per_s": round(rate, 1) if rate is not None else None,
                "remaining_rows": remaining,
                "eta_seconds": round(eta_seconds) if eta_seconds is not None else None,
                "terminal": terminal,
            },
        )
    prev_total = sum(prev_by_job.values()) if prev_by_job is not None else None
    agg_delta = (total - prev_total) if prev_total is not None else None
    agg_rate = (
        (agg_delta / dt_seconds) if (agg_delta is not None and dt_seconds > 0) else None
    )
    # Stall: we have a prior sample, jobs are still running, yet no rows moved.
    stalled = bool(
        prev_by_job is not None and nonterminal > 0 and agg_delta == 0,
    )
    return {
        "committed_rows": total,
        "delta_rows": agg_delta,
        "rows_per_s": round(agg_rate, 1) if agg_rate is not None else None,
        "stalled": stalled,
        "all_terminal": nonterminal == 0,
        "nonterminal_jobs": nonterminal,
        "jobs": per_job,
    }


def _print_throughput_report(report: dict) -> None:
    rate = report["rows_per_s"]
    rate_str = f"{rate} rows/s" if rate is not None else "baseline"
    delta = report["delta_rows"] if report["delta_rows"] is not None else 0
    stall = "  *** STALL ***" if report["stalled"] else ""
    print(
        f"[{utc_now_iso()[11:19]}] committed_rows={report['committed_rows']} "
        f"(+{delta}) => {rate_str} "
        f"nonterminal_jobs={report['nonterminal_jobs']}{stall}",
        flush=True,
    )
    for j in report["jobs"]:
        moved = bool(j["delta_rows"])
        pending = (not j["terminal"]) and bool(j["remaining_rows"])
        if not (moved or pending):
            continue
        progress = f"/{j['expected_rows']}" if j["expected_rows"] else ""
        eta = f" eta={j['eta_seconds']}s" if j["eta_seconds"] is not None else ""
        print(
            f"    job={j['job_id'][:12]} status={j['status']} "
            f"rows={j['committed_rows']}{progress} "
            f"(+{j['delta_rows'] or 0}){eta}",
            flush=True,
        )


async def cmd_throughput(args: argparse.Namespace) -> None:
    """Live checkpoint-based ingest throughput monitor."""
    _apply_runtime_overrides(args)
    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)
    if args.job_id:
        job_ids = [args.job_id]
    else:
        job_ids = list(job_store.read_dispatch(args.dispatch_id).job_ids)

    interval = max(1.0, float(args.interval))
    prev_by_job: dict[str, int] | None = None
    prev_clock: float | None = None
    iteration = 0
    if not args.json:
        print(
            f"=== throughput monitor: {len(job_ids)} job(s), "
            f"interval={interval:.0f}s ===",
            flush=True,
        )
    while True:
        clock = time.monotonic()
        current = _collect_throughput_sample(infra, job_ids)
        dt = (clock - prev_clock) if prev_clock is not None else interval
        report = _build_throughput_report(
            current=current,
            prev_by_job=prev_by_job,
            dt_seconds=dt,
        )
        if args.json:
            print(json.dumps({"ts": utc_now_iso(), **report}, default=str), flush=True)
        else:
            _print_throughput_report(report)

        prev_by_job = {j["job_id"]: int(j["committed_rows"]) for j in current}
        prev_clock = clock
        iteration += 1
        if report["all_terminal"]:
            if not args.json:
                print("All jobs terminal; throughput monitor done.", flush=True)
            break
        if args.iterations and iteration >= args.iterations:
            break
        await asyncio.sleep(interval)


async def cmd_worker_refresh_check(args: argparse.Namespace) -> None:
    """Fail closed when worker restart would interrupt active ingestion."""
    _apply_runtime_overrides(args)
    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)
    counts: dict[str, int] = {}
    jobs: list[dict] = []
    for manifest in job_store.list_dispatches(limit=args.dispatch_limit):
        for job_id in manifest.job_ids:
            snap = _load_job_snapshot(infra, job_id, include_events=False)
            counts[snap.derived_status] = counts.get(snap.derived_status, 0) + 1
            if (
                snap.derived_status
                in {
                    "running-active",
                    "running-stale",
                    "dlq",
                    "partial-dlq",
                }
                or snap.durable_status == "queued"
            ):
                jobs.append(
                    {
                        "job_id": job_id,
                        "dispatch_id": manifest.dispatch_id,
                        "durable_status": snap.durable_status,
                        "derived_status": snap.derived_status,
                        "next_action": snap.next_action,
                    },
                )
    blockers = []
    for job in jobs:
        if job["derived_status"] == "running-active" and args.allow_active:
            continue
        if job["durable_status"] == "queued" and args.allow_queued:
            continue
        if (
            job["derived_status"] == "running-active"
            or job["durable_status"] == "queued"
            or job["derived_status"] in {"running-stale", "dlq", "partial-dlq"}
        ):
            blockers.append(job)
    safe = not blockers or bool(args.force)
    result = {
        "safe_to_refresh": safe,
        "forced": bool(args.force),
        "allow_active": bool(args.allow_active),
        "allow_queued": bool(args.allow_queued),
        "counts": counts,
        "blockers": blockers[:50],
    }
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(
            f"safe_to_refresh={str(safe).lower()} forced={str(args.force).lower()}",
            flush=True,
        )
        print(f"counts={json.dumps(counts, sort_keys=True)}", flush=True)
        for job in blockers[:20]:
            print(
                f"  BLOCK job={job['job_id']} dispatch={job['dispatch_id']} "
                f"status={job['derived_status']} durable={job['durable_status']} "
                f"next={job['next_action']}",
                flush=True,
            )
    if not safe:
        sys.exit(2)


async def cmd_cancel(args: argparse.Namespace) -> None:
    """Cancel a dispatch (all non-terminal jobs) or a single job."""
    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)

    if args.job_id:
        try:
            job = job_store.cancel_job(args.job_id, reason=args.reason)
            print(f"Cancelled job {args.job_id}: {job.cancel_reason}")
            print("Workers will detect cancellation on their next checkpoint.")
        except FileNotFoundError:
            print(f"Job {args.job_id} not found")
            sys.exit(1)
        return

    try:
        manifest = job_store.read_dispatch(args.dispatch_id)
    except Exception:
        print(f"Dispatch {args.dispatch_id} not found")
        sys.exit(1)

    cancelled = 0
    skipped = 0
    for jid in manifest.job_ids:
        try:
            job = job_store.read_job(jid)
            if job.status in ("success", "error", "cancelled"):
                skipped += 1
                continue
            job_store.cancel_job(jid, reason=args.reason)
            cancelled += 1
        except Exception:
            logger.debug("Could not cancel job %s", jid)
            skipped += 1

    print(
        f"Dispatch {args.dispatch_id}: cancelled {cancelled} job(s), "
        f"skipped {skipped} (already terminal)",
    )
    if cancelled:
        print("Workers will detect cancellation on their next checkpoint.")


async def cmd_pause(args: argparse.Namespace) -> None:
    """Pause a dispatch (or single job) and drain its ingest backlog.

    The flow is:

    1. Flip ``status`` in GCS to ``paused`` for the targeted job(s) /
       dispatch via :class:`GcsDeploymentJobStore`.
    2. Drain the ingest subscription in bounded batches, parking
       payloads that belong to a paused job and nacking everything else
       so non-paused work continues to flow.
    3. Print a summary so the operator knows how many messages were
       moved into the parking lot and how many remain on the
       subscription (which the workers themselves will park when their
       control watchers trip ``pause_event``).
    """
    from unity_deploy.infra.gcp.message_parking import park_message
    from unity_deploy.infra.gcp.work_queue import PubSubWorkQueue

    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)
    work_queue = infra.work_queue
    artifact_store = infra.artifact_store
    assert isinstance(work_queue, PubSubWorkQueue)

    paused_jobs: set[str] = set()
    paused_dispatches: set[str] = set()

    if args.job_id:
        try:
            job = job_store.pause_job(args.job_id, reason=args.reason)
        except FileNotFoundError:
            print(f"Job {args.job_id} not found")
            sys.exit(1)
        if job.status != "paused":
            print(f"Job {args.job_id} is {job.status}, not paused (terminal state).")
            return
        paused_jobs.add(args.job_id)
        if job.dispatch_id:
            paused_dispatches.add(job.dispatch_id)
        print(f"Paused job {args.job_id}")
    else:
        try:
            manifest = job_store.read_dispatch(args.dispatch_id)
        except Exception:
            print(f"Dispatch {args.dispatch_id} not found")
            sys.exit(1)
        paused_count, skipped = job_store.pause_dispatch(
            args.dispatch_id,
            reason=args.reason,
        )
        paused_dispatches.add(args.dispatch_id)
        paused_jobs.update(manifest.job_ids)
        print(
            f"Dispatch {args.dispatch_id}: paused {paused_count} job(s), "
            f"skipped {skipped} (terminal or already paused)",
        )

    parked = 0
    released = 0
    for _batch in range(max(1, args.drain_batches)):
        items = await work_queue.receive(max_messages=100, topics=["ingest"])
        if not items:
            break
        for item in items:
            payload = item.payload or {}
            item_dispatch_id = str(payload.get("dispatch_id") or "")
            item_job_id = str(payload.get("job_id") or "")
            belongs_to_paused = item_job_id in paused_jobs or (
                item_dispatch_id and item_dispatch_id in paused_dispatches
            )
            if belongs_to_paused and item_dispatch_id:
                try:
                    park_message(
                        artifact_store,
                        dispatch_id=item_dispatch_id,
                        published_at=item.published_at,
                        message_id=item.message_id,
                        payload=payload,
                        topic="ingest",
                        parked_by="pipeline_control.pause",
                    )
                    await work_queue.ack(item.receipt_id)
                    parked += 1
                except Exception:
                    logger.exception("Failed to park message %s", item.message_id)
                    await work_queue.retry(
                        item.receipt_id,
                        error="park_failed",
                        delay_seconds=0,
                    )
            else:
                # Release back to the subscription so another pod can
                # process unrelated work without restarting the pod.
                await work_queue.retry(
                    item.receipt_id,
                    error="not-paused",
                    delay_seconds=0,
                )
                released += 1

    print(
        f"Drained ingest subscription: {parked} parked, "
        f"{released} released back for other consumers.",
    )
    if parked == 0 and released == 0:
        print(
            "No messages pulled by this drain pass. Running workers will "
            "park their own in-flight messages when their control "
            "watcher detects the pause (typically within ~2 seconds).",
        )
    print(
        "\nBacklog will drop to zero as workers finish parking their "
        "in-flight messages. Once num_undelivered_messages hits 0 the "
        "ingest HPA will scale the deployment down on its own.",
    )


async def cmd_resume(args: argparse.Namespace) -> None:
    """Resume a paused dispatch or job by re-publishing parked messages.

    Parked blobs are sorted lexicographically; because the key layout
    includes a zero-padded ``published_at_ns`` prefix, this is
    equivalent to sorting by original Pub/Sub publish order.  We
    re-publish serially and delete each blob only after its publish
    confirms, so a crash mid-resume is safely idempotent: re-running
    ``resume`` replays whatever is left in the parking lot.
    """
    from unity_deploy.infra.gcp.message_parking import (
        delete_parked,
        parked_payloads,
    )
    from unity_deploy.infra.gcp.work_queue import PubSubWorkQueue

    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)
    work_queue = infra.work_queue
    artifact_store = infra.artifact_store
    assert isinstance(work_queue, PubSubWorkQueue)

    dispatch_ids: list[str] = []
    job_filter: set[str] = set()

    if args.job_id:
        try:
            job = job_store.read_job(args.job_id)
        except FileNotFoundError:
            print(f"Job {args.job_id} not found")
            sys.exit(1)
        if not job.dispatch_id:
            print(
                f"Job {args.job_id} has no dispatch_id recorded; parked "
                f"messages (if any) cannot be located in GCS.",
            )
            sys.exit(1)
        if job.status in ("success", "error", "cancelled"):
            print(f"Job {args.job_id} is terminal ({job.status}); nothing to resume.")
            return
        dispatch_ids = [job.dispatch_id]
        job_filter = {args.job_id}
        job_store.resume_job(args.job_id)
        print(f"Resumed job {args.job_id}")
    else:
        try:
            manifest = job_store.read_dispatch(args.dispatch_id)
        except Exception:
            print(f"Dispatch {args.dispatch_id} not found")
            sys.exit(1)
        dispatch_ids = [manifest.dispatch_id]
        resumed_count, skipped = job_store.resume_dispatch(args.dispatch_id)
        print(
            f"Dispatch {args.dispatch_id}: resumed {resumed_count} job(s), "
            f"skipped {skipped} (not paused)",
        )

    republished = 0
    skipped_not_matching = 0
    for dispatch_id in dispatch_ids:
        for key, document in parked_payloads(artifact_store, dispatch_id):
            payload = document.get("payload") or {}
            item_job_id = str(payload.get("job_id") or "")
            if job_filter and item_job_id not in job_filter:
                skipped_not_matching += 1
                continue
            try:
                parked_job = job_store.read_job(item_job_id)
                if parked_job.status in ("success", "error", "cancelled"):
                    print(
                        f"Skipping terminal parked job {item_job_id} "
                        f"({parked_job.status}); deleting parked blob.",
                    )
                    delete_parked(artifact_store, key)
                    continue
            except Exception:
                logger.debug("Could not read parked job %s before resume", item_job_id)
            tombstone_key = f"{key}.resume.json"
            try:
                tombstone = artifact_store.get_json(tombstone_key)
                if (
                    isinstance(tombstone, dict)
                    and tombstone.get("status") == "published"
                ):
                    delete_parked(artifact_store, key)
                    continue
            except Exception:
                pass
            topic = str(document.get("topic") or "ingest")
            try:
                artifact_store.put_json(
                    tombstone_key,
                    {
                        "status": "publishing",
                        "parked_key": key,
                        "job_id": item_job_id,
                        "updated_at": utc_now_iso(),
                    },
                )
                message_id = await work_queue.publish(topic=topic, payload=payload)
                artifact_store.put_json(
                    tombstone_key,
                    {
                        "status": "published",
                        "parked_key": key,
                        "job_id": item_job_id,
                        "published_message_id": message_id,
                        "updated_at": utc_now_iso(),
                    },
                )
            except Exception:
                logger.exception("Failed to republish parked message %s", key)
                continue
            try:
                delete_parked(artifact_store, key)
            except Exception:
                logger.exception("Failed to delete parked blob %s", key)
            republished += 1

    print(
        f"Re-published {republished} parked message(s). "
        f"Skipped {skipped_not_matching} parked blob(s) for other jobs.",
    )
    if republished:
        print(
            "Backlog will climb as messages land on the ingest topic; "
            "HPA will scale the ingest deployment back up.",
        )


async def cmd_delete(args: argparse.Namespace) -> None:
    """Purge all GCS artifacts for a dispatch."""
    if not args.confirm:
        print(
            "This will permanently delete all GCS artifacts for the dispatch.\n"
            "Re-run with --confirm to proceed.",
        )
        sys.exit(1)

    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)

    try:
        manifest = job_store.read_dispatch(args.dispatch_id)
    except Exception:
        print(f"Dispatch {args.dispatch_id} not found")
        sys.exit(1)

    from unity_deploy.infra.gcp.artifact_store import GcsArtifactStore

    artifact_store = infra.artifact_store
    assert isinstance(artifact_store, GcsArtifactStore)
    bucket = infra.storage_client.bucket(infra.settings.artifact_store.bucket)

    total_deleted = 0
    for jid in manifest.job_ids:
        prefix = artifact_store._full_key(f"jobs/{jid}/")
        blobs = list(bucket.list_blobs(prefix=prefix))
        for blob in blobs:
            blob.delete()
            total_deleted += 1
        if blobs:
            logger.info("Deleted %d blob(s) for job %s", len(blobs), jid)

    dispatch_prefix = artifact_store._full_key(
        f"dispatches/{args.dispatch_id}/",
    )
    for blob in bucket.list_blobs(prefix=dispatch_prefix):
        blob.delete()
        total_deleted += 1

    print(
        f"Deleted {total_deleted} blob(s) across {len(manifest.job_ids)} job(s) "
        f"for dispatch {args.dispatch_id}",
    )


async def cmd_inspect(args: argparse.Namespace) -> None:
    """Inspect parse results: table schemas, sample rows, row counts."""
    from unity_deploy.infra.gcp.artifact_store import GcsArtifactStore

    infra = _init_infra(debug=args.debug)
    artifact_store = infra.artifact_store
    assert isinstance(artifact_store, GcsArtifactStore)

    bucket = infra.storage_client.bucket(infra.settings.artifact_store.bucket)
    prefix = artifact_store._full_key(f"jobs/{args.job_id}/manifests/")

    blobs = list(bucket.list_blobs(prefix=prefix))
    if not blobs:
        print(f"No parse manifests found for job {args.job_id}")
        return

    print(f"\nParse manifests for job {args.job_id}:")
    print(f"{'='*60}")

    for blob in blobs:
        if blob.name.endswith("_partial.json"):
            data = json.loads(blob.download_as_text(encoding="utf-8"))
            print(
                f"\n  [PARTIAL] status={data.get('status')}, reason={data.get('reason')}",
            )
            continue

        try:
            data = json.loads(blob.download_as_text(encoding="utf-8"))
        except Exception:
            continue

        file_path = data.get("file_path", "?")
        tables = data.get("tables", [])
        print(f"\n  File: {file_path}")
        print(f"  Status: {data.get('parse_status', '?')}")
        print(f"  Tables: {len(tables)}")

        for t in tables:
            tid = t.get("table_id", "?")
            handle = t.get("handle", {})
            cols = handle.get("columns", [])
            rows = handle.get("row_count")
            kind = handle.get("kind", "?")
            print(f"\n    Table: {tid}")
            print(f"    Handle: {kind}")
            if rows is not None:
                print(f"    Rows: {rows}")
            if cols:
                print(f"    Columns ({len(cols)}): {', '.join(cols[:15])}")
                if len(cols) > 15:
                    print(f"      ... and {len(cols) - 15} more")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        from unity_deploy.utils.load_repo_env import load_repo_dotenv

        load_repo_dotenv(override=False)
    except Exception:
        pass

    parser = _build_parser()
    args = parser.parse_args()

    commands = {
        "submit": cmd_submit,
        "list": cmd_list,
        "status": cmd_status,
        "monitor": cmd_monitor,
        "cancel": cmd_cancel,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "reconcile-dlq": cmd_reconcile_dlq,
        "retry": cmd_retry,
        "recover-stale": cmd_recover_stale,
        "reconcile-stale": cmd_reconcile_stale,
        "worker-refresh-check": cmd_worker_refresh_check,
        "delete": cmd_delete,
        "inspect": cmd_inspect,
        "verify": cmd_verify,
        "throughput": cmd_throughput,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
