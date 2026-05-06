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
    p_status.add_argument("--debug", action="store_true")

    # -- monitor -----------------------------------------------------------

    p_monitor = sub.add_parser("monitor", help="Monitor a single running job")
    p_monitor.add_argument("--job-id", required=True)
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

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    """Show per-job status for a dispatch."""
    infra = _init_infra(debug=args.debug)
    job_store = _get_job_store(infra)
    settings = infra.settings

    try:
        manifest = job_store.read_dispatch(args.dispatch_id)
    except Exception:
        print(f"Dispatch {args.dispatch_id} not found")
        return

    created = manifest.created_at[:19].replace("T", " ") if manifest.created_at else "?"
    print(f"\nDispatch: {manifest.dispatch_id}")
    print(f"Source:   {manifest.source}")
    print(f"Mode:     {manifest.mode}")
    print(f"Created:  {created}")
    print(f"Config:   {manifest.config_path or '(ad-hoc)'}")
    print(f"Files:    {manifest.total_files}")
    print()

    hdr = f"  {'JOB_ID':<34} {'STATUS':<12} {'STARTED':<20} {'FINISHED':<20}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    status_counts: dict[str, int] = {}
    for jid in manifest.job_ids:
        try:
            job = job_store.read_job(jid)
            st = job.status
            started = (job.started_at or "")[:19].replace("T", " ")
            finished = (job.finished_at or "")[:19].replace("T", " ")
        except Exception:
            st = "unknown"
            started = ""
            finished = ""
        status_counts[st] = status_counts.get(st, 0) + 1
        print(f"  {jid:<34} {st:<12} {started:<20} {finished:<20}")

    print()
    parts = []
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
            parts.append(f"{cnt} {s}")
    print(f"  Summary: {', '.join(parts)}")

    # Show run ledger entries for each job
    bucket = infra.storage_client.bucket(settings.artifact_store.bucket)
    prefix_str = settings.artifact_store.prefix.strip("/")
    for jid in manifest.job_ids:
        job_root = f"{prefix_str}/jobs/{jid}" if prefix_str else f"jobs/{jid}"
        ledger_key = f"{job_root}/run_ledger.jsonl"
        try:
            blob = bucket.blob(ledger_key)
            if not blob.exists():
                continue
            content = blob.download_as_text(encoding="utf-8")
            lines = [ln for ln in content.strip().split("\n") if ln.strip()]
            if lines:
                print(f"\n  Ledger for {jid[:12]}...:")
                for line in lines[-5:]:
                    entry = json.loads(line)
                    stage = entry.get("stage_name", "?")
                    status = entry.get("status", "?")
                    dur = entry.get("duration_ms", 0)
                    fp = Path(entry.get("file_path", "?")).name
                    print(f"    [{status}] {stage}: {fp} ({dur:.0f}ms)")
        except Exception:
            pass


async def cmd_monitor(args: argparse.Namespace) -> None:
    """Read progress and run ledger from GCS for a single job."""
    infra = _init_infra(debug=args.debug)
    settings = infra.settings

    while True:
        try:
            job = infra.job_store.read_job(args.job_id)
        except FileNotFoundError:
            print(f"Job {args.job_id} not found")
            return

        print(f"\n{'='*60}")
        print(f"Job: {job.job_id}")
        print(f"Status: {job.status}")
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

        if not args.follow or job.status in ("success", "error", "cancelled"):
            break

        print(f"\nPolling in {args.interval}s...")
        await asyncio.sleep(args.interval)


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
