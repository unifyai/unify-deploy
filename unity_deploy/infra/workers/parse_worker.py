"""Parse worker: consumes ParseRequested messages, runs FileParser, publishes IngestRequested."""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from google.cloud import storage

from unity.common.pipeline._utils import utc_now_iso
from unity.common.pipeline.artifact_store import ArtifactStore
from unity.common.pipeline.run_ledger import PipelineStageManifest
from unity.common.pipeline.types import IngestRequested, ParseRequested
from unity.common.pipeline.work_queue import ReceivedWorkItem

if TYPE_CHECKING:
    from unity.file_manager.file_parsers.types.contracts import FileParseResult

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
         :func:`unity.file_manager.parse_adapter.adapter.lower_to_ingest_plan`.
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
            local_paths: list[str] = []
            for file_uri in msg.file_paths:
                local_path = _download_source(
                    file_uri,
                    dest_dir=tmpdir,
                    storage_client=infra.storage_client,
                )
                local_paths.append(local_path)

            from unity.file_manager.file_parsers.file_parser import FileParser
            from unity.file_manager.file_parsers.types.contracts import FileParseRequest

            parser = FileParser()
            requests = [
                FileParseRequest(
                    source_local_path=lp,
                    logical_path=Path(lp).name,
                )
                for lp in local_paths
            ]

            parse_results = parser.parse_batch(
                requests,
                raises_on_error=False,
            )

            parse_duration = time.perf_counter() - parse_start

            if is_shutdown_requested() or await work_queue.is_cancelled(run_id):
                logger.info("[parse] Cancelled after parsing, job=%s", run_id)
                _write_partial_manifest(
                    artifact_store,
                    run_id,
                    parse_results,
                    "cancelled_after_parse",
                )
                return

            from unity.file_manager.parse_adapter.adapter import (
                lower_to_ingest_plan,
            )
            from unity.file_manager.types.config import FilePipelineConfig

            # Default worker config is sufficient here -- the plan only
            # needs ``ingest.business_contexts`` for lowering enrichment
            # (absent in the worker path today) and the artifact format
            # is driven by ``msg.artifact_format``.
            plan_config = FilePipelineConfig()

            for pr in parse_results:
                if pr.status != "success":
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
                    continue

                plan = lower_to_ingest_plan(
                    pr,
                    run_id=run_id,
                    config=plan_config,
                    artifact_store=artifact_store,
                    artifact_format=msg.artifact_format,
                )

                manifest_key = (
                    f"jobs/{run_id}/manifests/{Path(pr.logical_path).stem}.json"
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
                    deployment_id=msg.deployment_id,
                    manifest_key=manifest_key,
                    attachment_callback=msg.attachment_callback,
                    ingestion_mode=msg.ingestion_mode,
                    fm_binding=msg.fm_binding,
                    dm_binding=msg.dm_binding,
                )
                await work_queue.publish(
                    topic="ingest",
                    payload=ingest_msg.model_dump(mode="json"),
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
        filename = Path(blob_name).name

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
