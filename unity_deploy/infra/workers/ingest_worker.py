"""Ingest worker: consumes IngestRequested messages, streams rows into DataManager.

The worker consumes a pointer-only :class:`IngestPlan` that the parse
worker has already materialised to GCS.  Heavy data (content rows,
table row bodies) lives behind handles, so the manifest this worker
downloads is always KB-scale regardless of the source file size.

Two ingestion flavours are supported, driven by
``IngestRequested.ingestion_mode``:

- **FM mode** (``ingestion_mode="fm"``): activates a Unify context
  derived from ``msg.fm_binding``, instantiates a
  :class:`unity.file_manager.managers.file_manager.FileManager`, and
  delegates the work to
  :func:`unity.file_manager.managers.utils.executor.fm_process_plan`.
  The resulting rows land under ``Files/{alias}/{storage_id}/...`` with
  a proper ``FileRecords`` entry.

- **DM mode** (``ingestion_mode="dm"``): drives
  :func:`unity.common.pipeline.ingest_artifacts` directly over the
  plan's ``table_inputs``, issuing
  ``DataManager.ingest(ctx, None, table_input_handle=handle, ...)`` per
  table.  No ``FileRecords`` entry is created.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from unity.common.pipeline import (
    ArtifactWorkItem,
    IngestPlan,
    PipelineInstrumentation,
    ingest_artifacts,
)
from unity.common.pipeline._utils import utc_now_iso
from unity.common.pipeline.run_ledger import PipelineStageManifest
from unity.common.pipeline.types import (
    AttachmentCallback,
    IngestRequested,
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
    """Process one ``IngestRequested`` message end-to-end.

    Steps
    -----
    1. Read the ``IngestPlan`` manifest from the artifact store and
       rehydrate it into a Pydantic model.
    2. Branch on ``msg.ingestion_mode``:
       - ``"fm"``: activate the Unify context implied by ``fm_binding``,
         build a ``FileManager`` and call ``fm_process_plan``.
       - ``"dm"``: fan out table ingestion via ``ingest_artifacts`` with
         a DM-flavoured ``ingest_fn`` that streams from the plan's
         ``TableInputHandle``.
    3. Emit run-ledger entries and optionally publish an
       ``attachment_ingestion_complete`` envelope when the message
       originated from a CM attachment dispatch.
    """
    from .worker_utils import is_shutdown_requested

    msg = IngestRequested.model_validate(item.payload)
    run_id = msg.job_id

    artifact_store = infra.artifact_store
    work_queue = infra.work_queue
    job_store = infra.job_store
    run_ledger = infra.run_ledger_factory(run_id)
    cost_ledger = infra.cost_ledger_factory(run_id)

    logger.info(
        "[ingest] Starting job=%s, manifest=%s, mode=%s",
        run_id,
        msg.manifest_key,
        msg.ingestion_mode,
    )
    ingest_start = time.perf_counter()

    overall_error: str | None = None
    total_rows = 0
    try:
        manifest_payload: dict = artifact_store.get_json(msg.manifest_key)
        plan = IngestPlan.model_validate(manifest_payload)
        file_path = plan.file_path

        if is_shutdown_requested() or await work_queue.is_cancelled(run_id):
            logger.info("[ingest] Cancelled before dispatch, job=%s", run_id)
            run_ledger.write(
                PipelineStageManifest(
                    run_id=run_id,
                    file_path=file_path,
                    stage_name="ingest",
                    status="error",
                    error="cancelled",
                ),
            )
            overall_error = "cancelled"

        if overall_error is None:
            if msg.ingestion_mode == "fm":
                total_rows, overall_error = await _run_fm_mode(
                    plan=plan,
                    msg=msg,
                    infra=infra,
                    run_ledger=run_ledger,
                )
            else:
                total_rows, overall_error = await _run_dm_mode(
                    plan=plan,
                    msg=msg,
                    infra=infra,
                    run_ledger=run_ledger,
                )

        try:
            job = job_store.read_job(run_id)
            if job.status != "cancelled":
                job.status = "success" if overall_error is None else "error"
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
                success=overall_error is None,
                error=overall_error,
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


# ---------------------------------------------------------------------------
# FM mode
# ---------------------------------------------------------------------------


async def _run_fm_mode(
    *,
    plan: IngestPlan,
    msg: IngestRequested,
    infra: WorkerInfra,
    run_ledger,
) -> tuple[int, str | None]:
    """Dispatch an ``IngestPlan`` through ``fm_process_plan``.

    Activates the Unify context described by ``msg.fm_binding``, builds
    a ``FileManager`` bound to the requested alias, and delegates the
    entire per-file fanout (content + tables) to
    :func:`fm_process_plan`.  The function returns ``(total_rows,
    error)`` so the caller can still record an ``ingest`` ledger line
    and emit an attachment callback.
    """
    fm_binding = msg.fm_binding
    if fm_binding is None:
        raise RuntimeError("FM ingest mode requires msg.fm_binding to be set.")

    from .worker_utils import activate_unify_context

    activate_unify_context(
        user_id=fm_binding.user_id,
        assistant_id=fm_binding.assistant_id,
    )

    from unity.data_manager import DataManager
    from unity.file_manager.filesystem_adapters.local_adapter import (
        LocalFileSystemAdapter,
    )
    from unity.file_manager.managers.file_manager import FileManager
    from unity.file_manager.managers.utils.executor import fm_process_plan
    from unity.file_manager.types.config import FilePipelineConfig

    dm = DataManager()
    # The FM adapter's ``name`` determines the ``Files/{alias}/...``
    # namespace rows are written under.  ``LocalFileSystemAdapter.name``
    # is "Local", which matches what CM attachments produce today.  The
    # worker does not need the file bytes on disk (content/tables are
    # streamed from object-store handles), so ``enable_sync=False`` and a
    # throwaway root are sufficient.
    adapter = LocalFileSystemAdapter(root=None, enable_sync=False)
    fm = FileManager(adapter=adapter, data_manager=dm)
    logger.info(
        "[ingest][fm] activated context=%s/%s alias=%s",
        fm_binding.user_id,
        fm_binding.assistant_id,
        adapter.name,
    )

    config = FilePipelineConfig()
    instrumentation = PipelineInstrumentation.from_config(
        config,
        run_id=msg.job_id,
        file_count=1,
    )

    total_rows = 0
    error: str | None = None
    start = time.perf_counter()
    try:
        with instrumentation:
            result = fm_process_plan(
                fm,
                plan=plan,
                file_path=plan.file_path,
                config=config,
                instrumentation=instrumentation,
                reporter=None,
                enable_progress=False,
                verbosity="low",
            )
            status = str(getattr(result, "status", "error") or "error")
            if status != "success":
                error = str(getattr(result, "error", "fm_process_plan failed") or "")
            total_rows = _extract_total_rows(result)
    except Exception as exc:
        error = str(exc) or "fm_process_plan raised"
        logger.exception("[ingest][fm] Failed for %s", plan.file_path)

    run_ledger.write(
        PipelineStageManifest(
            run_id=msg.job_id,
            file_path=plan.file_path,
            stage_name="ingest",
            status="success" if error is None else "error",
            duration_ms=(time.perf_counter() - start) * 1000,
            error=error,
            meta={
                "ingestion_mode": "fm",
                "fm_alias": fm_binding.fm_alias,
                "total_rows": total_rows,
            },
        ),
    )
    return total_rows, error


def _extract_total_rows(result: Any) -> int:
    """Best-effort total-row extraction from a ``FileResultType`` result."""
    metrics = getattr(result, "metrics", None)
    if metrics is not None:
        for attr in ("rows_ingested", "rows_inserted", "total_rows"):
            value = getattr(metrics, attr, None)
            if isinstance(value, int):
                return value
    total = getattr(result, "total_records", None)
    if isinstance(total, int):
        return total
    return 0


# ---------------------------------------------------------------------------
# DM mode
# ---------------------------------------------------------------------------


async def _run_dm_mode(
    *,
    plan: IngestPlan,
    msg: IngestRequested,
    infra: WorkerInfra,
    run_ledger,
) -> tuple[int, str | None]:
    """Dispatch an ``IngestPlan`` via raw DataManager ingestion.

    Constructs one ``ArtifactWorkItem`` per table in the plan, then
    drives :func:`unity.common.pipeline.ingest_artifacts` with a DM
    ``ingest_fn`` that calls ``dm.ingest(ctx, None,
    table_input_handle=handle, ...)``.  Streaming is preserved --
    ``dm.ingest`` pulls batches from the handle itself rather than
    loading the entire table into memory.
    """
    dm_binding = msg.dm_binding
    default_target = (
        dm_binding.target_context if dm_binding else msg.target_context
    ) or msg.job_id

    from unity.data_manager import DataManager
    from unity.file_manager.types.config import FilePipelineConfig

    dm = DataManager()
    config = FilePipelineConfig()
    instrumentation = PipelineInstrumentation.from_config(
        config,
        run_id=msg.job_id,
        file_count=1,
    )

    work_items: list[ArtifactWorkItem] = []
    for meta in plan.tables_meta:
        table_id = str(meta.table_id or "")
        handle = (plan.table_inputs or {}).get(table_id)
        if handle is None:
            continue
        columns = list(meta.columns or []) or list(
            getattr(handle, "columns", []) or [],
        )
        row_count = int(meta.row_count or getattr(handle, "row_count", 0) or 0)
        target_context = default_target
        if msg.dm_binding is None and table_id:
            target_context = f"{default_target}/{table_id}"
        work_items.append(
            ArtifactWorkItem(
                kind="table",
                label=str(meta.label or table_id or "table"),
                stage_name="ingest_table",
                payload={
                    "dm": dm,
                    "context": target_context,
                    "handle": handle,
                    "batch_size": msg.batch_size,
                },
                columns=columns,
                row_count=row_count,
                table_id=table_id or None,
                stage_id=instrumentation.make_stage_id(
                    file_path=plan.file_path,
                    stage_name="ingest_table",
                    discriminator=table_id or str(meta.label or ""),
                ),
                meta={
                    "row_count": row_count,
                    "table_label": meta.label,
                    "column_count": len(columns),
                    "source_handle_type": type(handle).__name__,
                    "context": target_context,
                },
            ),
        )

    def _dm_ingest_fn(item: ArtifactWorkItem) -> dict:
        pl = item.payload
        result = pl["dm"].ingest(
            pl["context"],
            None,
            table_input_handle=pl["handle"],
            chunk_size=pl["batch_size"],
        )
        return {
            "ingest_result": result,
            "context": pl["context"],
            "row_count": getattr(result, "rows_inserted", 0) or 0,
        }

    total_rows = 0
    error: str | None = None
    start = time.perf_counter()
    try:
        with instrumentation:
            artifact_results = ingest_artifacts(
                work_items=work_items,
                ingest_fn=_dm_ingest_fn,
                instrumentation=instrumentation,
                source_path=plan.file_path,
                max_workers=getattr(config.execution, "max_embed_workers", 8),
                retry_config=config.retry,
            )
        for ar in artifact_results:
            if ar.success:
                total_rows += int(
                    getattr(ar, "rows_inserted", 0)
                    or (
                        ar.value.get("row_count", 0)
                        if isinstance(ar.value, dict)
                        else 0
                    ),
                )
            elif error is None:
                error = ar.error or "ingest failed"
    except Exception as exc:
        error = str(exc) or "ingest_artifacts raised"
        logger.exception("[ingest][dm] Failed for %s", plan.file_path)

    run_ledger.write(
        PipelineStageManifest(
            run_id=msg.job_id,
            file_path=plan.file_path,
            stage_name="ingest",
            status="success" if error is None else "error",
            duration_ms=(time.perf_counter() - start) * 1000,
            error=error,
            meta={
                "ingestion_mode": "dm",
                "default_target_context": default_target,
                "table_count": len(work_items),
                "total_rows": total_rows,
            },
        ),
    )
    return total_rows, error


# ---------------------------------------------------------------------------
# Attachment callback
# ---------------------------------------------------------------------------


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
