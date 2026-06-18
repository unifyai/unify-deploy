#!/usr/bin/env python3
"""Enumerate and bulk-cancel/delete all pipeline jobs in the GCS artifact bucket.

Useful for cleaning up jobs dispatched before batch tracking (dispatch_id)
existed.  Operates directly on ``jobs/*/job.json`` blobs so it works
regardless of whether a DispatchManifest exists.

Usage
-----
    # List every job and its status (dry-run, no mutations)
    python -m unity_deploy.scripts.cancel_all_jobs --bucket droid-pipeline-artifacts-staging list

    # Cancel every non-terminal job (sets status="cancelled" on job.json)
    python -m unity_deploy.scripts.cancel_all_jobs --bucket droid-pipeline-artifacts-staging cancel

    # Delete ALL job artifacts from the bucket (irreversible!)
    python -m unity_deploy.scripts.cancel_all_jobs --bucket droid-pipeline-artifacts-staging delete --confirm
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from droid.common.pipeline._utils import utc_now_iso

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"success", "error", "cancelled"})


def _init(*, bucket_override: str | None = None):
    if bucket_override:
        os.environ["DROID_GCS_ARTIFACT_BUCKET"] = bucket_override

    from unity_deploy.infra.workers.worker_utils import (
        build_worker_infra,
        initialize_worker_environment,
    )

    initialize_worker_environment(debug=True)
    infra = build_worker_infra()

    from unity_deploy.infra.gcp.deployment_stores import GcsDeploymentJobStore

    job_store = infra.job_store
    assert isinstance(job_store, GcsDeploymentJobStore)
    print(f"Bucket: {job_store._store._bucket_name}")
    return job_store


def _iter_job_blobs(job_store):
    """Yield (blob, job_id) for every ``jobs/*/job.json`` in the bucket."""
    prefix = job_store._store._full_key("jobs/")
    bucket = job_store._store.bucket
    for blob in bucket.list_blobs(prefix=prefix):
        if not blob.name.endswith("/job.json"):
            continue
        parts = blob.name.split("/")
        idx = parts.index("jobs") if "jobs" in parts else -1
        if idx < 0 or idx + 1 >= len(parts):
            continue
        yield blob, parts[idx + 1]


def cmd_list(job_store) -> None:
    """Print every job with its status."""
    counts: dict[str, int] = {}
    jobs = []
    for blob, job_id in _iter_job_blobs(job_store):
        try:
            data = json.loads(blob.download_as_text(encoding="utf-8"))
            status = data.get("status", "unknown")
            created = data.get("created_at", "?")
            dispatch_id = data.get("dispatch_id", "")
        except Exception:
            status, created, dispatch_id = "unreadable", "?", ""
        counts[status] = counts.get(status, 0) + 1
        jobs.append((job_id, status, created, dispatch_id))

    if not jobs:
        print("No jobs found in bucket.")
        return

    jobs.sort(key=lambda t: t[2])
    w = max(len(j[0]) for j in jobs)
    for job_id, status, created, did in jobs:
        did_part = f"  dispatch={did}" if did else ""
        print(f"  {job_id:<{w}}  {status:<12}  {created}{did_part}")

    print()
    for s in ("queued", "running", "success", "error", "cancelled", "unreadable"):
        if s in counts:
            print(f"  {counts[s]:>4} {s}")
    print(f"  {len(jobs):>4} total")


def cmd_cancel(job_store) -> None:
    """Cancel every non-terminal job."""
    cancelled = 0
    skipped = 0
    for blob, job_id in _iter_job_blobs(job_store):
        try:
            data = json.loads(blob.download_as_text(encoding="utf-8"))
            status = data.get("status", "unknown")
        except Exception:
            skipped += 1
            continue

        if status in TERMINAL_STATES:
            skipped += 1
            continue

        data["status"] = "cancelled"
        data["cancelled_at"] = utc_now_iso()
        data["cancel_reason"] = "bulk-cancel-script"
        blob.upload_from_string(
            json.dumps(data, ensure_ascii=False, default=str),
            content_type="application/json",
        )
        cancelled += 1
        print(f"  cancelled {job_id} (was {status})")

    print(f"\nDone: {cancelled} cancelled, {skipped} skipped (already terminal).")
    if cancelled:
        print("Workers will detect cancellation on their next checkpoint.")


def cmd_scan(job_store) -> None:
    """List ALL blobs in the bucket, showing status for any job.json found."""
    bucket = job_store._store.bucket
    count = 0
    for blob in bucket.list_blobs(max_results=500):
        suffix = ""
        if blob.name.endswith("/job.json"):
            try:
                data = json.loads(blob.download_as_text(encoding="utf-8"))
                status = data.get("status", "?")
                created = data.get("created_at", "")
                suffix = f"  [status={status}  created={created}]"
            except Exception:
                suffix = "  [unreadable]"
        print(f"  {blob.name}{suffix}")
        count += 1
    if count == 0:
        print("Bucket is completely empty.")
    else:
        print(f"\n  {count} blob(s) shown (capped at 500).")


def cmd_delete(job_store, *, confirm: bool) -> None:
    """Delete ALL job and dispatch artifacts from the bucket."""
    if not confirm:
        print("Pass --confirm to actually delete. This is irreversible.")
        sys.exit(1)

    deleted = 0
    prefix = job_store._store._full_key("jobs/")
    bucket = job_store._store.bucket
    for blob in bucket.list_blobs(prefix=prefix):
        blob.delete()
        deleted += 1
        if deleted % 50 == 0:
            print(f"  deleted {deleted} blobs...")

    dispatch_prefix = job_store._store._full_key("dispatches/")
    for blob in bucket.list_blobs(prefix=dispatch_prefix):
        blob.delete()
        deleted += 1

    deploy_prefix = job_store._store._full_key("deployments/")
    for blob in bucket.list_blobs(prefix=deploy_prefix):
        blob.delete()
        deleted += 1

    print(f"\nDeleted {deleted} blob(s) across jobs/, dispatches/, deployments/.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cancel_all_jobs",
        description="Bulk list / cancel / delete pipeline jobs in the GCS artifact bucket.",
    )
    parser.add_argument(
        "--bucket",
        default=None,
        help=(
            "Override the GCS artifact bucket "
            "(e.g. droid-pipeline-artifacts-staging). "
            "Defaults to DROID_GCS_ARTIFACT_BUCKET env var."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List all jobs with status")
    sub.add_parser("scan", help="Raw-list all blobs in the bucket (up to 200)")
    sub.add_parser("cancel", help="Cancel all non-terminal jobs")
    p_delete = sub.add_parser("delete", help="Delete ALL artifacts (irreversible)")
    p_delete.add_argument("--confirm", action="store_true", required=True)

    args = parser.parse_args()
    job_store = _init(bucket_override=args.bucket)

    if args.command == "list":
        cmd_list(job_store)
    elif args.command == "scan":
        cmd_scan(job_store)
    elif args.command == "cancel":
        cmd_cancel(job_store)
    elif args.command == "delete":
        cmd_delete(job_store, confirm=args.confirm)


if __name__ == "__main__":
    main()
