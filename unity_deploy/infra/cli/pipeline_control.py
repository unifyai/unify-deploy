#!/usr/bin/env python3
"""Operator CLI for GCP pipeline job control.

Commands:
    submit   -- Upload source files to GCS and publish ParseRequested (1 per file)
    list     -- List recent dispatches
    status   -- Show per-job status for a dispatch
    monitor  -- Read progress and run ledger from GCS for a single job
    cancel   -- Cancel a dispatch or single job
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

    from unity_deploy.customization.scripts.ingest_utils import load_pipeline_config

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

        try:
            result = publish_parse_request(
                target=target,
                logical_path=file_path,
                ingestion_mode=args.mode,
                fm_binding=fm_binding,
                dm_binding=dm_binding,
                dispatch_id=dispatch_id,
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
        for s in ("success", "running", "queued", "error", "cancelled", "unknown"):
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
    for s in ("success", "running", "queued", "error", "cancelled", "unknown"):
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
        from unity_deploy.load_repo_env import load_repo_dotenv

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
