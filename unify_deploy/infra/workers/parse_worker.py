"""Parse worker: consumes ParseRequested messages, runs FileParser, publishes IngestRequested."""

from __future__ import annotations

import hashlib
import logging
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from google.cloud import storage

from unify.common.pipeline._utils import utc_now_iso
from unify.common.pipeline.artifact_store import ArtifactStore
from unify.common.pipeline.run_ledger import PipelineStageManifest
from unify.common.pipeline.types import IngestRequested, ParseRequested
from unify.common.pipeline.work_queue import ReceivedWorkItem
from unify_deploy.infra.gcp.artifact_store import (
    LeaseNotAcquired,
    LeaseRecord,
    _is_not_found_error,
)
from .worker_utils import DuplicateLiveAttempt

if TYPE_CHECKING:
    from unify.file_manager.file_parsers.types.contracts import FileParseResult

    from .worker_utils import WorkerInfra

logger = logging.getLogger(__name__)


async def handle_parse_message(
    item: ReceivedWorkItem,
    *,
    infra: WorkerInfra,
) -> None:
    """Process one ParseRequested message end-to-end.

    Steps:
      1. Download source file(s) from GCS to temp directory
      2. Parse via FileParser.parse_batch()
      3. Cancellation checkpoint
      4. Lower each parse result into a pointer-only ``IngestPlan`` via
         :func:`unify.file_manager.parse_adapter.adapter.lower_to_ingest_plan`.
         Heavy outputs (content rows lowered from the ``DocumentGraph``,
         inline table rows) are materialised to GCS as JSONL artifacts and
         referenced by handles so the manifest published to the queue
         stays KB-scale regardless of input size.
      5. Write the ``IngestPlan`` manifest (pointers only) to GCS.
      6. Emit run/cost ledger entries.
      7. Publish ``IngestRequested`` carrying the manifest key + the
         propagated ``ingestion_mode`` / binding metadata.
    """
    from .worker_utils import is_shutdown_requested

    msg = ParseRequested.model_validate(item.payload)
    run_id = msg.job_id

    artifact_store = infra.artifact_store
    work_queue = infra.work_queue
    job_store = infra.job_store
    run_ledger = infra.run_ledger_factory(run_id)
    cost_ledger = infra.cost_ledger_factory(run_id)

    try:
        job = job_store.read_job(run_id)
    except Exception:
        job = None
    if job is not None:
        status = str(getattr(job, "status", "") or "")
        if status in {"success", "error", "cancelled"}:
            logger.info(
                "[parse] Job=%s already terminal (%s); no-op",
                run_id,
                status,
            )
            run_ledger.close()
            cost_ledger.close()
            return

    if len(msg.file_paths) != 1:
        run_ledger.close()
        cost_ledger.close()
        raise RuntimeError(
            "ParseRequested must contain exactly one file_path for durable "
            f"outbox semantics; received {len(msg.file_paths)}.",
        )

    try:
        if await _replay_parse_outbox_if_needed(artifact_store, work_queue, run_id):
            run_ledger.close()
            cost_ledger.close()
            return
    except Exception:
        run_ledger.close()
        cost_ledger.close()
        raise

    parse_attempt_id = uuid.uuid4().hex
    parse_owner = f"parse:{uuid.uuid4().hex[:12]}"
    try:
        parse_lease: LeaseRecord = artifact_store.acquire_lease(
            f"jobs/{run_id}/leases/parse.json",
            owner_id=parse_owner,
            attempt_id=parse_attempt_id,
            stage="parse",
            ttl_seconds=900,
        )
    except LeaseNotAcquired as exc:
        logger.info(
            "[parse] Duplicate live attempt for job=%s owner=%s expires_at=%s; "
            "acking duplicate message",
            run_id,
            exc.lease.owner_id if exc.lease else "?",
            exc.lease.expires_at if exc.lease else "?",
        )
        run_ledger.close()
        cost_ledger.close()
        raise DuplicateLiveAttempt(str(exc), stage="parse", lease=exc.lease) from exc

    logger.info("[parse] Starting job=%s, files=%d", run_id, len(msg.file_paths))
    parse_start = time.perf_counter()

    try:
        try:
            job = job_store.read_job(run_id)
            job.status = "running"
            job.started_at = utc_now_iso()
            job_store.upsert_job(job)
        except Exception:
            logger.debug("Could not update job status for %s", run_id)

        with tempfile.TemporaryDirectory(prefix="parse_worker_") as tmpdir:
            local_entries: list[tuple[str, str]] = []
            for file_uri in msg.file_paths:
                local_path = _download_source(
                    file_uri,
                    dest_dir=tmpdir,
                    storage_client=infra.storage_client,
                )
                local_entries.append((local_path, _logical_name_from_uri(file_uri)))

            from unify.file_manager.file_parsers.file_parser import FileParser
            from unify.file_manager.file_parsers.types.contracts import FileParseRequest

            parser = FileParser()
            requests = [
                FileParseRequest(
                    source_local_path=lp,
                    logical_path=logical_name,
                )
                for lp, logical_name in local_entries
            ]

            parse_results = parser.parse_batch(
                requests,
                raises_on_error=False,
            )

            parse_duration = time.perf_counter() - parse_start

            failed_results = [pr for pr in parse_results if pr.status != "success"]
            if failed_results:
                for pr in failed_results:
                    run_ledger.write(
                        PipelineStageManifest(
                            run_id=run_id,
                            file_path=pr.logical_path,
                            stage_name="parse",
                            status="error",
                            duration_ms=parse_duration * 1000,
                            error=pr.error,
                        ),
                    )
                raise RuntimeError(
                    "Parse failed for configured source files: "
                    + ", ".join(pr.logical_path for pr in failed_results),
                )

            if is_shutdown_requested() or await work_queue.is_cancelled(run_id):
                logger.info("[parse] Cancelled after parsing, job=%s", run_id)
                _write_partial_manifest(
                    artifact_store,
                    run_id,
                    parse_results,
                    "cancelled_after_parse",
                )
                return

            from unify.file_manager.parse_adapter.adapter import (
                lower_to_ingest_plan,
            )
            from unify.file_manager.types.config import FilePipelineConfig

            # Default worker config is sufficient here -- the plan only
            # needs ``ingest.business_contexts`` for lowering enrichment
            # (absent in the worker path today) and the artifact format
            # is driven by ``msg.artifact_format``.
            plan_config = FilePipelineConfig()

            for source_index, (file_uri, pr) in enumerate(
                zip(msg.file_paths, parse_results),
            ):
                parse_lease = _refresh_parse_lease(
                    artifact_store,
                    run_id=run_id,
                    lease=parse_lease,
                )
                source_gs_uri = file_uri if file_uri.startswith("gs://") else ""
                plan = lower_to_ingest_plan(
                    pr,
                    run_id=run_id,
                    config=plan_config,
                    artifact_store=artifact_store,
                    artifact_format=msg.artifact_format,
                    source_gs_uri=source_gs_uri,
                )

                if msg.table_config:
                    plan = _merge_table_config(plan, msg.table_config)

                manifest_key = (
                    f"jobs/{run_id}/manifests/"
                    f"{source_index:04d}-{_safe_manifest_stem(pr.logical_path)}.json"
                )
                parse_lease = _refresh_parse_lease(
                    artifact_store,
                    run_id=run_id,
                    lease=parse_lease,
                )
                artifact_store.put_json(manifest_key, plan.model_dump(mode="json"))

                run_ledger.write(
                    PipelineStageManifest(
                        run_id=run_id,
                        file_path=pr.logical_path,
                        stage_name="parse",
                        status="success",
                        duration_ms=parse_duration * 1000,
                        meta={
                            "table_count": len(plan.tables_meta),
                            "manifest_key": manifest_key,
                            "ingestion_mode": msg.ingestion_mode,
                            "has_content_rows": plan.content_rows_handle is not None,
                        },
                    ),
                )

                ingest_msg = IngestRequested(
                    job_id=run_id,
                    dispatch_id=msg.dispatch_id,
                    manifest_key=manifest_key,
                    parse_outbox_key=_parse_outbox_key(run_id),
                    attachment_callback=msg.attachment_callback,
                    ingestion_mode=msg.ingestion_mode,
                    fm_binding=msg.fm_binding,
                    dm_binding=msg.dm_binding,
                )
                parse_lease = _refresh_parse_lease(
                    artifact_store,
                    run_id=run_id,
                    lease=parse_lease,
                )
                artifact_store.put_json(
                    _parse_outbox_key(run_id),
                    {
                        "status": "publish_pending",
                        "job_id": run_id,
                        "manifest_key": manifest_key,
                        "payload": ingest_msg.model_dump(mode="json"),
                        "updated_at": utc_now_iso(),
                    },
                )
                parse_lease = _refresh_parse_lease(
                    artifact_store,
                    run_id=run_id,
                    lease=parse_lease,
                )
                message_id = await work_queue.publish(
                    topic="ingest",
                    payload=ingest_msg.model_dump(mode="json"),
                )
                parse_lease = _refresh_parse_lease(
                    artifact_store,
                    run_id=run_id,
                    lease=parse_lease,
                )
                artifact_store.put_json(
                    _parse_outbox_key(run_id),
                    {
                        "status": "published",
                        "job_id": run_id,
                        "manifest_key": manifest_key,
                        "payload": ingest_msg.model_dump(mode="json"),
                        "published_message_id": message_id,
                        "updated_at": utc_now_iso(),
                    },
                )
                logger.info(
                    "[parse] Published IngestRequested for %s (mode=%s, tables=%d, content=%s)",
                    pr.logical_path,
                    msg.ingestion_mode,
                    len(plan.tables_meta),
                    "yes" if plan.content_rows_handle is not None else "no",
                )

        run_ledger.flush()
        cost_ledger.flush()
        logger.info(
            "[parse] Completed job=%s in %.1fs",
            run_id,
            time.perf_counter() - parse_start,
        )

    except Exception:
        logger.exception("[parse] Failed job=%s", run_id)
        raise
    finally:
        run_ledger.close()
        cost_ledger.close()


def _parse_outbox_key(run_id: str) -> str:
    return f"jobs/{run_id}/outbox/parse.json"


def _refresh_parse_lease(
    artifact_store,
    *,
    run_id: str,
    lease: LeaseRecord,
) -> LeaseRecord:
    return artifact_store.refresh_lease(
        f"jobs/{run_id}/leases/parse.json",
        owner_id=lease.owner_id,
        attempt_id=lease.attempt_id,
        generation=lease.generation,
        ttl_seconds=900,
    )


async def _replay_parse_outbox_if_needed(
    artifact_store,
    work_queue,
    run_id: str,
) -> bool:
    try:
        outbox = artifact_store.get_json(_parse_outbox_key(run_id))
    except Exception as exc:
        if _is_not_found_error(exc):
            return False
        raise
    if not isinstance(outbox, dict):
        raise ValueError(f"Parse outbox for job={run_id} is not a JSON object")
    status = str(outbox.get("status") or "")
    if status == "published":
        logger.info("[parse] Job=%s already published ingest message; no-op", run_id)
        return True
    payload = outbox.get("payload")
    if status == "publish_pending" and isinstance(payload, dict):
        message_id = await work_queue.publish(topic="ingest", payload=payload)
        outbox["status"] = "published"
        outbox["published_message_id"] = message_id
        outbox["updated_at"] = utc_now_iso()
        artifact_store.put_json(_parse_outbox_key(run_id), outbox)
        logger.info("[parse] Reconciled publish_pending outbox for job=%s", run_id)
        return True
    return False


def _safe_manifest_stem(logical_path: str) -> str:
    stem = Path(logical_path).stem or "source"
    digest = hashlib.sha256(str(logical_path).encode("utf-8")).hexdigest()[:12]
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem)
    return f"{safe}-{digest}"


def _logical_name_from_uri(file_uri: str) -> str:
    if file_uri.startswith("gs://"):
        from urllib.parse import urlparse

        parsed = urlparse(file_uri)
        return Path(parsed.path.lstrip("/")).name or "source"
    return Path(file_uri).name or "source"


def _merge_table_config(plan, table_config: dict):
    """Merge per-table config from ParseRequested into IngestPlan.tables_meta.

    Matches config entries to TableMeta by sheet_name, label, or table_id. Returns
    a new plan with updated tables_meta carrying the config fields that
    the ingest worker needs (description, embed_columns, etc.).
    """
    from unify.common.pipeline.types import TableMeta

    unmatched = set(table_config)
    updated: list[TableMeta] = []
    single_table_fallback = len(plan.tables_meta) == 1 and len(table_config) == 1
    for meta in plan.tables_meta:
        keys = [
            key
            for key in (meta.sheet_name, meta.label, meta.table_id)
            if key is not None
        ]
        matched_key = next((key for key in keys if key in table_config), None)
        if matched_key is None and single_table_fallback:
            matched_key = next(iter(table_config))
        cfg = table_config.get(matched_key, {}) if matched_key is not None else {}
        if not cfg:
            updated.append(meta)
            continue
        unmatched.discard(matched_key)
        updated.append(
            meta.model_copy(
                update={
                    "context": cfg.get("context") or meta.context,
                    "description": cfg.get("description") or meta.description,
                    "column_descriptions": cfg.get("column_descriptions")
                    or meta.column_descriptions,
                    "embed_columns": cfg.get("embed_columns") or meta.embed_columns,
                    "embed_strategy": cfg.get("embed_strategy", meta.embed_strategy),
                    "chunk_size": cfg.get("chunk_size", meta.chunk_size),
                    "post_ingest": cfg.get("post_ingest") or meta.post_ingest,
                },
            ),
        )
    if unmatched:
        raise ValueError(
            "Table config contains entries that did not match parsed tables: "
            + ", ".join(sorted(str(k) for k in unmatched)),
        )
    return plan.model_copy(update={"tables_meta": updated})


def _download_source(
    file_uri: str,
    *,
    dest_dir: str,
    storage_client: storage.Client,
) -> str:
    """Download a gs:// URI to a local temp file and return its path."""
    if file_uri.startswith("gs://"):
        from urllib.parse import urlparse

        parsed = urlparse(file_uri)
        bucket_name = parsed.netloc
        blob_name = parsed.path.lstrip("/")
        source_hash = hashlib.sha256(blob_name.encode("utf-8")).hexdigest()[:12]
        source_name = Path(blob_name).name or "source"
        filename = f"{source_hash}-{source_name}"

        import os
        import time as _time

        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        local_path = str(Path(dest_dir) / filename)
        t0 = _time.perf_counter()
        blob.download_to_filename(local_path)
        elapsed = _time.perf_counter() - t0
        mb = os.path.getsize(local_path) / (1024 * 1024)
        rate = mb / elapsed if elapsed > 0 else 0
        logger.info(
            "Downloaded %s -> %s (%.1f MB in %.1fs, %.1f MB/s)",
            file_uri,
            local_path,
            mb,
            elapsed,
            rate,
        )
        return local_path

    return file_uri


def _write_partial_manifest(
    artifact_store: ArtifactStore,
    run_id: str,
    parse_results: list[FileParseResult],
    reason: str,
) -> None:
    """Write a partial manifest when the job is cancelled mid-flight."""
    manifest = {
        "run_id": run_id,
        "status": "cancelled",
        "reason": reason,
        "files_parsed": len(parse_results),
        "files_succeeded": sum(1 for r in parse_results if r.status == "success"),
    }
    try:
        artifact_store.put_json(
            f"jobs/{run_id}/manifests/_partial.json",
            manifest,
        )
    except Exception:
        logger.debug("Could not write partial manifest for %s", run_id)
