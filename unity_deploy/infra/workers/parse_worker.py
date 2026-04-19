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
      4. Write ParsedFileBundle manifest to GCS
      5. Materialize table artifacts to GCS
      6. Emit run/cost ledger entries
      7. Publish IngestRequested for each file
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

            from unity.common.pipeline.transport import build_table_handles

            for pr in parse_results:
                if pr.status != "success" or not pr.tables:
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

                handles = build_table_handles(
                    pr,
                    artifact_store=artifact_store,
                    artifact_format=msg.artifact_format,
                )

                manifest_data = {
                    "run_id": run_id,
                    "file_path": pr.logical_path,
                    "tables": [
                        {
                            "table_id": tid,
                            "handle": h.model_dump(mode="json"),
                        }
                        for tid, h in handles.items()
                    ],
                    "parse_status": pr.status,
                    "table_count": len(pr.tables),
                }

                manifest_key = f"{run_id}/manifests/{Path(pr.logical_path).stem}.json"
                artifact_store.put_json(manifest_key, manifest_data)

                run_ledger.write(
                    PipelineStageManifest(
                        run_id=run_id,
                        file_path=pr.logical_path,
                        stage_name="parse",
                        status="success",
                        duration_ms=parse_duration * 1000,
                        meta={
                            "table_count": len(pr.tables),
                            "manifest_key": manifest_key,
                        },
                    ),
                )

                ingest_msg = IngestRequested(
                    job_id=run_id,
                    deployment_id=msg.deployment_id,
                    manifest_key=manifest_key,
                    attachment_callback=msg.attachment_callback,
                )
                await work_queue.publish(
                    topic="ingest",
                    payload=ingest_msg.model_dump(mode="json"),
                )
                logger.info(
                    "[parse] Published IngestRequested for %s",
                    pr.logical_path,
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

        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        local_path = str(Path(dest_dir) / filename)
        blob.download_to_filename(local_path)
        logger.info("Downloaded %s -> %s", file_uri, local_path)
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
        artifact_store.put_json(f"{run_id}/manifests/_partial.json", manifest)
    except Exception:
        logger.debug("Could not write partial manifest for %s", run_id)
