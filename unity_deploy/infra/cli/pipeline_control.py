#!/usr/bin/env python3
"""Operator CLI for GCP pipeline job control.

Commands:
    submit   -- Upload source files to GCS and publish ParseRequested
    monitor  -- Read progress and run ledger from GCS
    cancel   -- Set job status to cancelled
    inspect  -- Read parse manifests and display table schemas/samples

Usage:
    python -m unity_deploy.infra.cli.pipeline_control submit \\
        --config path/to/pipeline_config.json --project MyProject
    python -m unity_deploy.infra.cli.pipeline_control monitor --job-id <id>
    python -m unity_deploy.infra.cli.pipeline_control cancel --job-id <id>
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

    p_submit = sub.add_parser("submit", help="Submit a parse+ingest job")
    p_submit.add_argument(
        "--config",
        required=True,
        help="Path to pipeline_config.json",
    )
    p_submit.add_argument("--project", required=True, help="Unify project name")
    p_submit.add_argument("--deployment", default="v1")
    p_submit.add_argument("--client", default="")
    p_submit.add_argument(
        "--job-id",
        default="",
        help="Custom job ID (auto-generated if empty)",
    )
    p_submit.add_argument("--debug", action="store_true")

    p_monitor = sub.add_parser("monitor", help="Monitor a running job")
    p_monitor.add_argument("--job-id", required=True)
    p_monitor.add_argument(
        "--follow",
        action="store_true",
        help="Poll until completion",
    )
    p_monitor.add_argument("--interval", type=float, default=10.0)
    p_monitor.add_argument("--debug", action="store_true")

    p_cancel = sub.add_parser("cancel", help="Cancel a running job")
    p_cancel.add_argument("--job-id", required=True)
    p_cancel.add_argument("--reason", default="operator-initiated")
    p_cancel.add_argument("--debug", action="store_true")

    p_inspect = sub.add_parser("inspect", help="Inspect parse results for a job")
    p_inspect.add_argument("--job-id", required=True)
    p_inspect.add_argument("--debug", action="store_true")

    return parser


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def cmd_submit(args: argparse.Namespace) -> None:
    """Upload source files and publish ParseRequested."""
    from unity.common.pipeline.deployment.types import (
        DeploymentBundleRef,
        DeploymentIngestionJob,
    )
    from unity.common.pipeline.types import ParseRequested

    from unity_deploy.customization.scripts.ingest_utils import load_pipeline_config
    from unity_deploy.infra.gcp.artifact_store import GcsArtifactStore
    from unity_deploy.infra.workers.worker_utils import (
        build_worker_infra,
        initialize_worker_environment,
    )

    initialize_worker_environment(debug=args.debug)
    infra = build_worker_infra()

    config = load_pipeline_config(args.config)
    job_id = args.job_id or uuid4().hex
    artifact_store = infra.artifact_store
    assert isinstance(artifact_store, GcsArtifactStore)

    gcs_file_uris: list[str] = []
    for sf in config.source_files:
        file_path = sf.file_path
        if not Path(file_path).exists():
            logger.warning("Source file not found, skipping: %s", file_path)
            continue
        blob_key = artifact_store._full_key(
            f"{job_id}/sources/{Path(file_path).name}",
        )
        bucket = artifact_store.bucket
        blob = bucket.blob(blob_key)
        blob.upload_from_filename(file_path)
        gcs_uri = f"gs://{artifact_store._bucket_name}/{blob_key}"
        gcs_file_uris.append(gcs_uri)
        logger.info("Uploaded %s -> %s", file_path, gcs_uri)

    job = DeploymentIngestionJob(
        job_id=job_id,
        bundle_ref=DeploymentBundleRef(bundle_id=job_id, manifest_path=""),
        run_mode="data_manager",
        execution_target="local_with_gcp",
        status="queued",
    )
    infra.job_store.upsert_job(job)

    msg = ParseRequested(
        job_id=job_id,
        deployment_id=(
            f"{args.client}/{args.deployment}" if args.client else args.deployment
        ),
        file_paths=gcs_file_uris,
    )
    await infra.work_queue.publish(topic="parse", payload=msg.model_dump(mode="json"))

    print(f"\nJob submitted: {job_id}")
    print(f"  Files: {len(gcs_file_uris)}")
    print(
        f"  Monitor: python -m unity_deploy.infra.cli.pipeline_control monitor --job-id {job_id}",
    )
    print(
        f"  Cancel:  python -m unity_deploy.infra.cli.pipeline_control cancel --job-id {job_id}",
    )


async def cmd_monitor(args: argparse.Namespace) -> None:
    """Read progress and run ledger from GCS."""
    from unity_deploy.infra.workers.worker_utils import (
        build_worker_infra,
        initialize_worker_environment,
    )

    initialize_worker_environment(debug=args.debug)
    infra = build_worker_infra()
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

        env = settings.environment
        prefix = settings.ledger.prefix.strip("/")
        env_prefix = f"{prefix}/{env}" if prefix else env
        ledger_key = f"{env_prefix}/{args.job_id}/run_ledger.jsonl"
        try:
            bucket = infra.storage_client.bucket(settings.ledger.bucket)
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
    """Cancel a running job."""
    from unity_deploy.infra.gcp.deployment_stores import GcsDeploymentJobStore
    from unity_deploy.infra.workers.worker_utils import (
        build_worker_infra,
        initialize_worker_environment,
    )

    initialize_worker_environment(debug=args.debug)
    infra = build_worker_infra()
    job_store = infra.job_store
    assert isinstance(job_store, GcsDeploymentJobStore)

    try:
        job = job_store.cancel_job(args.job_id, reason=args.reason)
        print(f"Cancelled job {args.job_id}: {job.cancel_reason}")
        print("Workers will detect cancellation on their next checkpoint.")
    except FileNotFoundError:
        print(f"Job {args.job_id} not found")
        sys.exit(1)


async def cmd_inspect(args: argparse.Namespace) -> None:
    """Inspect parse results: table schemas, sample rows, row counts."""
    from unity_deploy.infra.gcp.artifact_store import GcsArtifactStore
    from unity_deploy.infra.workers.worker_utils import (
        build_worker_infra,
        initialize_worker_environment,
    )

    initialize_worker_environment(debug=args.debug)
    infra = build_worker_infra()
    artifact_store = infra.artifact_store
    assert isinstance(artifact_store, GcsArtifactStore)

    bucket = infra.storage_client.bucket(infra.settings.artifact_store.bucket)
    prefix = artifact_store._full_key(f"{args.job_id}/manifests/")

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
        "monitor": cmd_monitor,
        "cancel": cmd_cancel,
        "inspect": cmd_inspect,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    asyncio.run(handler(args))


if __name__ == "__main__":
    main()
