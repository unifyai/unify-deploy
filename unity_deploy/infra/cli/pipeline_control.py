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
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
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


def _load_job_snapshot(infra, job_id: str):
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
    events = list_job_events(artifact_store, job_id)
    heartbeat_at = latest_heartbeat_at(artifact_store, job_id)
    leases = read_active_leases(artifact_store, job_id)
    derived_status, retry_classification, retry_eligible = derive_status(
        durable_status=durable_status,
        dlq_records=dlq_records,
        checkpoints=checkpoints,
        heartbeat_at=heartbeat_at,
        leases=leases,
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
    elif derived_status == "running-stale":
        next_action = (
            f"pipeline_control retry --job-id {job_id} --only stale-running --dry-run"
        )
        queue_location = "none"
    elif durable_status == "queued":
        next_action = "wait for parse/ingest backlog or inspect queue metrics"
        queue_location = "parse/ingest"
    else:
        next_action = ""
        queue_location = "none"
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


def _resolve_embed_columns(config, file_path: str, sheet_name: str):
    """Look up embed source columns for a file + sheet from config."""
    for spec in config.embed.file_specs:
        if spec.file_path in file_path or spec.file_path == "*":
            for table_spec in spec.tables:
                if table_spec.table == sheet_name:
                    return list(table_spec.source_columns)
    return None


def _resolve_column_descriptions(config, sheet_name: str) -> dict:
    """Look up column descriptions from business_contexts config."""
    if not config.ingest.business_contexts:
        return {}
    for fc in config.ingest.business_contexts.file_contexts:
        for tc in fc.table_contexts:
            if tc.table == sheet_name and tc.column_descriptions:
                return dict(tc.column_descriptions)
    return {}


def _build_table_config_for_sf(config, sf) -> dict:
    """Build per-table config dict from PipelineConfig for a SourceFileSpec."""
    embed_strategy = config.embed.strategy or "off"
    result = {}
    for spec in sf.tables:
        embed_cols = _resolve_embed_columns(config, sf.file_path, spec.sheet)
        col_descs = _resolve_column_descriptions(config, spec.sheet)
        post_ingest = config.effective_post_ingest(spec)

        entry: dict = {
            "description": spec.description or None,
            "embed_columns": embed_cols,
            "embed_strategy": embed_strategy if embed_cols else "off",
            "chunk_size": spec.chunk_size,
        }
        if col_descs:
            entry["column_descriptions"] = col_descs
        if post_ingest is not None:
            entry["post_ingest"] = post_ingest.model_dump(mode="json")
        result[spec.sheet] = entry
    return result


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

        table_config = _build_table_config_for_sf(config, sf) if sf.tables else None

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
    print(f"  List:    pipeline_control list")
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
    snapshots = [_load_job_snapshot(infra, jid) for jid in manifest.job_ids]
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

    created = manifest.created_at[:19].replace("T", " ") if manifest.created_at else "?"
    print(f"\nDispatch: {manifest.dispatch_id}")
    print(f"Source:   {manifest.source}")
    print(f"Mode:     {manifest.mode}")
    print(f"Created:  {created}")
    print(f"Config:   {manifest.config_path or '(ad-hoc)'}")
    print(f"Files:    {manifest.total_files}")
    print("\nEnvironment / Resources:")
    for key, value in resources.items():
        print(f"  {key}: {value or '(unset)'}")
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

    print()
    ordered_counts = ", ".join(
        f"{count} {status}" for status, count in sorted(counts.items())
    )
    print(f"  Summary: {ordered_counts or 'no jobs'}")

    retryable = [snap for snap in snapshots if snap.retry_eligible]
    non_retryable_dlq = [
        snap for snap in snapshots if snap.dlq_records and not snap.retry_eligible
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

        snap = _load_job_snapshot(infra, args.job_id)
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
        inner = (
            payload.get("payload")
            if isinstance(payload.get("payload"), dict)
            else payload
        )
        job_id = str(inner.get("job_id") or "")
        dispatch_id = str(inner.get("dispatch_id") or "")
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
            try:
                previous_status = infra.job_store.read_job(job_id).status
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

    plan: list[dict] = []
    skipped: list[dict] = []
    for job_id in target_jobs:
        snap = _load_job_snapshot(infra, job_id)
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
                    }
                    job_store.upsert_job(job)
                write_job_event(
                    artifact_store,
                    event.model_copy(
                        update={
                            "event_type": "retry_published",
                            "pubsub_message_id": message_id,
                            "next_action": "queued",
                        },
                    ),
                )
                published.append({"job_id": item["job_id"], "message_id": message_id})
            except Exception as exc:
                write_job_event(
                    artifact_store,
                    event.model_copy(
                        update={
                            "event_type": "retry_publish_failed",
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "next_action": "kept_prior_state",
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
    print("Retry resources:")
    for key, value in resources.items():
        print(f"  {key}: {value or '(unset)'}")
    print(f"\nMode: {'EXECUTE' if execute else 'DRY-RUN'}")
    print(f"Would retry {len(plan)} job(s); skipped {len(skipped)}.")
    for item in plan:
        print(
            f"  RETRY job={item['job_id']} topic={item['topic']} "
            f"checkpoint_rows={item['checkpoint_rows']} "
            f"attempt={item['retry_attempt']} reasons={','.join(item['reasons'])}",
        )
    for item in skipped[:20]:
        print(f"  SKIP job={item['job_id']} reason={item['reason']}")


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
        "delete": cmd_delete,
        "inspect": cmd_inspect,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
