"""Ingest worker: consumes IngestRequested messages, streams rows into DataManager."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from unity.common.pipeline._utils import utc_now_iso
from unity.common.pipeline.row_streaming import iter_table_input_row_batches
from unity.common.pipeline.run_ledger import PipelineStageManifest
from unity.common.pipeline.types import (
    AttachmentCallback,
    CsvFileHandle,
    IngestRequested,
    InlineRowsHandle,
    ObjectStoreArtifactHandle,
    TableInputHandle,
    XlsxSheetHandle,
)
from unity.common.pipeline.work_queue import ReceivedWorkItem

if TYPE_CHECKING:
    from .worker_utils import WorkerInfra

logger = logging.getLogger(__name__)


async def handle_ingest_message(
    item: ReceivedWorkItem,
    *,
    infra: WorkerInfra,
) -> None:
    """Process one IngestRequested message end-to-end.

    Steps:
      1. Read ParsedFileBundle manifest from GCS
      2. For each table, check cancellation, then stream rows via dm.ingest()
      3. Emit run/cost ledger entries
    """
    from .worker_utils import is_shutdown_requested

    msg = IngestRequested.model_validate(item.payload)
    run_id = msg.job_id

    artifact_store = infra.artifact_store
    work_queue = infra.work_queue
    job_store = infra.job_store
    run_ledger = infra.run_ledger_factory(run_id)
    cost_ledger = infra.cost_ledger_factory(run_id)

    logger.info("[ingest] Starting job=%s, manifest=%s", run_id, msg.manifest_key)
    ingest_start = time.perf_counter()

    table_failures: list[str] = []
    try:
        manifest: dict = artifact_store.get_json(msg.manifest_key)
        tables: list[dict] = manifest.get("tables", [])
        file_path: str = manifest.get("file_path", "")

        logger.info("[ingest] %d table(s) to ingest from %s", len(tables), file_path)

        total_rows = 0
        for table_entry in tables:
            table_id: str = table_entry.get("table_id", "")
            handle_data: dict = table_entry.get("handle", {})

            if is_shutdown_requested() or await work_queue.is_cancelled(run_id):
                logger.info(
                    "[ingest] Cancelled before table %s, job=%s",
                    table_id,
                    run_id,
                )
                run_ledger.write(
                    PipelineStageManifest(
                        run_id=run_id,
                        file_path=file_path,
                        table_id=table_id,
                        stage_name="ingest",
                        status="error",
                        error="cancelled",
                    ),
                )
                table_failures.append("cancelled")
                break

            table_start = time.perf_counter()

            try:
                handle = _resolve_handle(handle_data)
                context = msg.target_context or f"{run_id}/{table_id}"

                from unity.data_manager import DataManager

                dm = DataManager()
                rows_for_ingest: list[dict] = []

                for batch in iter_table_input_row_batches(
                    handle,
                    batch_size=msg.batch_size,
                ):
                    rows_for_ingest.extend(batch)

                inserted = 0
                if rows_for_ingest:
                    result = dm.ingest(
                        context,
                        rows_for_ingest,
                        chunk_size=msg.batch_size,
                    )
                    inserted = getattr(result, "rows_inserted", 0) or 0
                    total_rows += inserted

                table_duration = (time.perf_counter() - table_start) * 1000

                run_ledger.write(
                    PipelineStageManifest(
                        run_id=run_id,
                        file_path=file_path,
                        table_id=table_id,
                        stage_name="ingest",
                        status="success",
                        duration_ms=table_duration,
                        meta={
                            "rows_inserted": inserted,
                            "context": context,
                        },
                    ),
                )
                logger.info(
                    "[ingest] Table %s: %d rows in %.1fms",
                    table_id,
                    inserted,
                    table_duration,
                )

            except Exception as exc:
                table_duration = (time.perf_counter() - table_start) * 1000
                logger.exception("[ingest] Table %s failed", table_id)
                run_ledger.write(
                    PipelineStageManifest(
                        run_id=run_id,
                        file_path=file_path,
                        table_id=table_id,
                        stage_name="ingest",
                        status="error",
                        duration_ms=table_duration,
                        error=str(exc),
                    ),
                )
                table_failures.append(str(exc))

        try:
            job = job_store.read_job(run_id)
            if job.status != "cancelled":
                job.status = "success" if not table_failures else "error"
                job.finished_at = utc_now_iso()
                job.metadata["total_rows_inserted"] = total_rows
                job_store.upsert_job(job)
        except Exception:
            logger.debug("Could not update job status for %s", run_id)

        run_ledger.flush()
        cost_ledger.flush()

        logger.info(
            "[ingest] Completed job=%s, %d rows in %.1fs",
            run_id,
            total_rows,
            time.perf_counter() - ingest_start,
        )

        if msg.attachment_callback is not None:
            await _publish_attachment_completion(
                callback=msg.attachment_callback,
                success=not table_failures,
                error=table_failures[0] if table_failures else None,
            )

    except Exception as exc:
        logger.exception("[ingest] Failed job=%s", run_id)
        if msg.attachment_callback is not None:
            try:
                await _publish_attachment_completion(
                    callback=msg.attachment_callback,
                    success=False,
                    error=str(exc) or "ingest worker failed",
                )
            except Exception:
                logger.exception(
                    "[ingest] Failed to publish attachment completion for job=%s",
                    run_id,
                )
        raise
    finally:
        run_ledger.close()
        cost_ledger.close()


def _resolve_handle(handle_data: dict) -> TableInputHandle:
    """Reconstruct a TableInputHandle from its serialized form."""
    kind = handle_data.get("kind", "")
    if kind == "object_store_artifact":
        return ObjectStoreArtifactHandle.model_validate(handle_data)
    if kind == "csv_file":
        return CsvFileHandle.model_validate(handle_data)
    if kind == "xlsx_sheet":
        return XlsxSheetHandle.model_validate(handle_data)
    if kind == "inline_rows":
        return InlineRowsHandle.model_validate(handle_data)
    raise ValueError(f"Unknown handle kind: {kind!r}")


async def _publish_attachment_completion(
    *,
    callback: AttachmentCallback,
    success: bool,
    error: str | None,
) -> None:
    """Publish ``thread="attachment_ingestion_complete"`` to the assistant topic.

    The CM's ``CommsManager`` subscribes to ``unity-{assistant_id}{env_suffix}``
    and routes this envelope to
    ``attachment_ingestion.apply_attachment_completion`` which updates
    ``FileRecords`` for the originating attachment.
    """
    import os

    from google.cloud import pubsub_v1

    project_id = os.environ.get("GCP_PROJECT_ID") or os.environ.get(
        "UNITY_PUBSUB_PROJECT_ID",
    )
    if not project_id:
        logger.warning(
            "GCP_PROJECT_ID not set; cannot publish attachment_ingestion_complete",
        )
        return

    topic_name = f"unity-{callback.assistant_id}{callback.env_suffix}"
    envelope = {
        "thread": "attachment_ingestion_complete",
        "publish_timestamp": time.time(),
        "event": {
            "display_name": callback.display_name,
            "status": "success" if success else "error",
            "error": error,
        },
    }

    def _sync_publish() -> str:
        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(project_id, topic_name)
        future = publisher.publish(
            topic_path,
            json.dumps(envelope, default=str).encode("utf-8"),
            thread="attachment_ingestion_complete",
        )
        return str(future.result(timeout=30))

    message_id = await asyncio.to_thread(_sync_publish)
    logger.info(
        "[ingest] Published attachment_ingestion_complete -> %s (msg_id=%s)",
        topic_name,
        message_id,
    )
