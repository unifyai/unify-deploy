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
import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from unity.common.pipeline import (
    ArtifactWorkItem,
    CancellationCheck,
    IngestPlan,
    PipelineCancelled,
    PipelineInstrumentation,
    ingest_artifacts,
)
from unity.common.pipeline._utils import utc_now_iso
from unity.common.pipeline.run_ledger import PipelineStageManifest
from unity.common.pipeline.types import (
    AttachmentCallback,
    CsvFileHandle,
    IngestBinding,
    IngestCheckpoint,
    IngestRequested,
    ObjectStoreArtifactHandle,
    TableInputHandle,
    XlsxSheetHandle,
)
from unity.common.pipeline.work_queue import ReceivedWorkItem, RetryWorkItem

from .assistant_key_resolver import resolve_api_key
from unity_deploy.infra.gcp.artifact_store import (
    LeaseNotAcquired,
    LeaseRecord,
    StaleLeaseError,
)
from unity_deploy.infra.gcp.pipeline_observability import (
    PipelineJobEvent,
    make_receipt_hash,
    write_job_event,
)
from .worker_utils import DuplicateLiveAttempt

if TYPE_CHECKING:
    from .worker_utils import WorkerInfra

logger = logging.getLogger(__name__)

_TERMINAL_JOB_STATUSES = {"success", "error", "cancelled"}
_PRIVATE_INGEST_KEY = "_unity_ingest_key"


@dataclass
class _ActiveIngestLease:
    key: str
    owner_id: str
    attempt_id: str
    generation: int | None


# ---------------------------------------------------------------------------
# Per-message scratch staging for gs:// artifacts
# ---------------------------------------------------------------------------


def _stage_remote_handles(
    plan: IngestPlan,
    *,
    artifact_store: Any,
    scratch_dir: Path,
) -> IngestPlan:
    """Download remote-backed artifact handles into *scratch_dir*.

    JSONL ``ObjectStoreArtifactHandle`` handles with ``gs://`` URIs are
    intentionally **not** staged: ``row_streaming`` streams them
    directly from GCS via ``blob.open("r")`` when a ``storage_client``
    is supplied, eliminating ephemeral-storage pressure entirely.

    CSV/XLSX handles whose ``storage_uri`` is ``gs://`` need the source
    file downloaded so that row iteration can open the local copy.
    A dedup dict ensures each ``gs://`` source is downloaded at most once
    even when multiple handles (e.g. multiple XLSX sheets) share the
    same source file.

    Non-JSONL ``ObjectStoreArtifactHandle`` handles (future formats like
    parquet) are also staged.  Handles that already carry a usable
    ``source_local_path`` are left untouched.  The ``scratch_dir`` is
    owned by the caller and should be cleaned up in a ``finally`` block.
    """
    updates: dict[str, Any] = {}
    staged_files: dict[str, Path] = {}

    if plan.content_rows_handle is not None:
        staged_content = _stage_handle(
            plan.content_rows_handle,
            artifact_store=artifact_store,
            scratch_dir=scratch_dir,
            hint="content",
            staged_files=staged_files,
        )
        if staged_content is not plan.content_rows_handle:
            updates["content_rows_handle"] = staged_content

    if plan.table_inputs:
        staged_tables: dict[str, TableInputHandle] = {}
        any_change = False
        for table_id, handle in plan.table_inputs.items():
            staged = _stage_handle(
                handle,
                artifact_store=artifact_store,
                scratch_dir=scratch_dir,
                hint=table_id,
                staged_files=staged_files,
            )
            staged_tables[table_id] = staged
            if staged is not handle:
                any_change = True
        if any_change:
            updates["table_inputs"] = staged_tables

    if not updates:
        return plan
    return plan.model_copy(update=updates)


def _stage_handle(
    handle: TableInputHandle,
    *,
    artifact_store: Any,
    scratch_dir: Path,
    hint: str,
    staged_files: dict[str, Path] | None = None,
) -> TableInputHandle:
    """Stage a single handle locally when it needs local disk access.

    JSONL ``ObjectStoreArtifactHandle`` handles stream directly from GCS.
    CSV/XLSX handles with a ``gs://`` ``storage_uri`` need the source
    file downloaded.  The *staged_files* dict deduplicates downloads
    when multiple handles reference the same remote file.
    """
    if isinstance(handle, (CsvFileHandle, XlsxSheetHandle)):
        if not handle.storage_uri.startswith("gs://"):
            return handle
        return _stage_tabular_handle(
            handle,
            artifact_store=artifact_store,
            scratch_dir=scratch_dir,
            staged_files=staged_files if staged_files is not None else {},
        )

    if not isinstance(handle, ObjectStoreArtifactHandle):
        return handle
    if handle.source_local_path:
        return handle
    if not handle.storage_uri.startswith("gs://"):
        return handle
    if handle.artifact_format == "jsonl":
        return handle
    if not hasattr(artifact_store, "download_to_local"):
        raise RuntimeError(
            f"Cannot stage {handle.storage_uri!r}: artifact_store "
            f"{type(artifact_store).__name__} has no download_to_local() method.",
        )

    safe_hint = _safe_scratch_name(hint)
    dest = scratch_dir / f"{safe_hint}.{handle.artifact_format}"
    local_path = artifact_store.download_to_local(handle.storage_uri, dest)
    logger.info(
        "[ingest] Staged %s -> %s (%d bytes)",
        handle.storage_uri,
        local_path,
        local_path.stat().st_size if local_path.exists() else 0,
    )
    return handle.model_copy(update={"source_local_path": str(local_path)})


def _stage_tabular_handle(
    handle: CsvFileHandle | XlsxSheetHandle,
    *,
    artifact_store: Any,
    scratch_dir: Path,
    staged_files: dict[str, Path],
) -> CsvFileHandle | XlsxSheetHandle:
    """Download the source file for a CSV/XLSX handle, with dedup.

    Multiple XLSX sheets reference the same workbook file.  The
    *staged_files* dict maps ``gs://`` URIs to already-downloaded local
    paths so each source is fetched at most once per message.
    """
    gs_uri = handle.storage_uri
    if gs_uri in staged_files:
        return handle.model_copy(
            update={"source_local_path": str(staged_files[gs_uri])},
        )

    if not hasattr(artifact_store, "download_to_local"):
        raise RuntimeError(
            f"Cannot stage {gs_uri!r}: artifact_store "
            f"{type(artifact_store).__name__} has no download_to_local() method.",
        )

    suffix = Path(gs_uri).suffix or (
        ".csv" if isinstance(handle, CsvFileHandle) else ".xlsx"
    )
    dest = scratch_dir / f"{_safe_scratch_name(Path(gs_uri).stem)}{suffix}"
    local_path = artifact_store.download_to_local(gs_uri, dest)
    staged_files[gs_uri] = local_path
    logger.info(
        "[ingest] Staged tabular source %s -> %s (%d bytes)",
        gs_uri,
        local_path,
        local_path.stat().st_size if local_path.exists() else 0,
    )
    return handle.model_copy(update={"source_local_path": str(local_path)})


def _safe_scratch_name(value: str) -> str:
    text = str(value or "").strip() or "artifact"
    return "".join(
        char if char.isalnum() or char in ("-", "_") else "_" for char in text
    )


def _scratch_guard_threshold_bytes() -> int:
    """Return the per-worker /tmp guard threshold.

    Default is 3.5 GiB, matching a 4 GiB ephemeral-storage limit with a
    little headroom. Operators can tune it without rebuilding the image.
    """
    raw = os.environ.get("UNITY_INGEST_TMP_MAX_BYTES")
    if raw:
        try:
            return max(int(raw), 1)
        except ValueError:
            logger.warning(
                "[ingest] Ignoring invalid UNITY_INGEST_TMP_MAX_BYTES=%r",
                raw,
            )
    return int(3.5 * 1024 * 1024 * 1024)


def _scratch_dir_size_bytes(scratch_dir: Path) -> int:
    """Return the total file bytes owned by a per-message scratch dir.

    Do not use filesystem-wide stats here: in GKE the container's /tmp is
    backed by the node overlay filesystem, so df/statvfs includes shared
    container image/snapshot usage unrelated to this worker.
    """
    if not scratch_dir.exists():
        return 0

    total = 0
    for root, _, files in os.walk(scratch_dir):
        for name in files:
            path = Path(root) / name
            try:
                total += path.stat().st_size
            except OSError:
                # Cleanup can race with the guard; disappeared files no
                # longer contribute to scratch pressure.
                continue
    return total


def _guard_scratch_usage(
    *,
    scratch_dir: Path,
    run_id: str,
    phase: str,
) -> None:
    scratch_used = _scratch_dir_size_bytes(scratch_dir)
    threshold = _scratch_guard_threshold_bytes()
    if scratch_used <= threshold:
        return
    raise RuntimeError(
        "ingest worker scratch usage exceeded guard threshold "
        f"(job={run_id}, phase={phase}, scratch_used={scratch_used}, "
        f"threshold={threshold}, scratch_dir={scratch_dir})",
    )


def _delete_staged_scratch_files(scratch_dir: Path, *, run_id: str) -> None:
    """Delete staged source files as soon as durable ingest has finished."""
    if not scratch_dir.exists():
        return
    reclaimed = 0
    deleted = 0
    for path in sorted(scratch_dir.rglob("*"), reverse=True):
        try:
            if path.is_file():
                size = path.stat().st_size
                path.unlink()
                reclaimed += size
                deleted += 1
            elif path.is_dir() and path != scratch_dir:
                path.rmdir()
        except OSError:
            logger.debug(
                "[ingest] Could not delete staged scratch path job=%s path=%s",
                run_id,
                path,
                exc_info=True,
            )
    if deleted:
        logger.info(
            "[ingest] Deleted %d staged scratch file(s) for job=%s, reclaimed %.1f MB",
            deleted,
            run_id,
            reclaimed / (1024 * 1024),
        )


# ---------------------------------------------------------------------------
# Control-event watcher (cancel + pause, instant)
# ---------------------------------------------------------------------------


@dataclass
class _ControlWatch:
    """Handle returned by :func:`_spawn_control_watcher`.

    ``cancel_event`` trips when ``job.json.status == "cancelled"``.
    ``pause_event`` trips when ``job.json.status == "paused"``.  Both
    events are latching: once set, they stay set for the remainder of
    the message's lifetime (a paused job that later gets resumed will
    be picked up again as a fresh Pub/Sub message, not this one).

    Callers pass ``is_cancelled`` (a ``() -> bool`` closure) down into
    ``ingest_artifacts`` / ``_make_checkpoint_callback``; the closure
    just reads ``event.is_set()`` and is safe to call from worker
    threads because it only touches plain attribute reads.
    """

    cancel_event: asyncio.Event
    pause_event: asyncio.Event
    _stop_event: asyncio.Event
    _task: asyncio.Task[None]

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set() or self.pause_event.is_set()

    def paused(self) -> bool:
        return self.pause_event.is_set()

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    async def stop(self) -> None:
        """Signal the watcher to exit and wait for it."""
        self._stop_event.set()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def _spawn_control_watcher(
    artifact_store: Any,
    run_id: str,
    *,
    interval: float = 1.5,
) -> _ControlWatch:
    """Start a background task that watches ``job.json`` for control flips.

    The task polls ``jobs/{run_id}/job.json`` every *interval* seconds
    and sets the matching event on the first status transition to
    ``cancelled`` or ``paused``.  Polling is bounded and cheap (a
    single GCS ``get_json`` per tick), and it replaces the older
    per-checkpoint cache where cancellation detection could lag by up
    to 60 s + the current chunk duration.

    The task self-terminates either when an event trips (no point
    polling further — the state is latched) or when the caller invokes
    :meth:`_ControlWatch.stop` in a ``finally`` block on the hot path.
    """
    cancel_event = asyncio.Event()
    pause_event = asyncio.Event()
    stop_event = asyncio.Event()

    async def _watch() -> None:
        while not stop_event.is_set():
            try:
                data = await asyncio.to_thread(
                    artifact_store.get_json,
                    f"jobs/{run_id}/job.json",
                )
                status = (data or {}).get("status")
                if status == "cancelled":
                    cancel_event.set()
                    return
                if status == "paused":
                    pause_event.set()
                    return
            except Exception:
                # Job record may not yet exist for a freshly-dispatched
                # message, or GCS may be transiently unavailable.
                # Either way, keep polling — the worst case is we miss
                # one tick of latency.
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    task = asyncio.create_task(_watch(), name=f"ctrl-watcher:{run_id}")
    return _ControlWatch(
        cancel_event=cancel_event,
        pause_event=pause_event,
        _stop_event=stop_event,
        _task=task,
    )


# ---------------------------------------------------------------------------
# Checkpoint callback factory
# ---------------------------------------------------------------------------


def _make_checkpoint_callback(
    artifact_store: Any,
    job_id: str,
    artifact_id: str,
    *,
    attempt_id: str = "",
    lease: _ActiveIngestLease | None = None,
    initial_rows: int = 0,
    initial_chunks: int = 0,
    total_rows: int | None = None,
    file_path: str = "",
    chunk_size: int | None = None,
    log_every_chunks: int = 10,
    is_cancelled: CancellationCheck | None = None,
):
    """Return an ``on_task_complete`` callback that writes GCS checkpoints.

    The callback is fired by ``PipelineExecutor._notify()`` after each
    pipeline task completes inside ``dm.ingest()``.  Only
    ``insert_chunk_*`` tasks trigger a checkpoint write; other task types
    (``create_table``, ``embed_*``, etc.) are ignored.

    When *is_cancelled* is supplied, the callback polls it after each
    chunk checkpoint and raises ``PipelineCancelled`` to unwind the
    executor's pipeline, giving per-chunk cancellation granularity.
    """
    state = {
        "rows": initial_rows,
        "chunks": initial_chunks,
        "last_rows": initial_rows,
        "last_logged_at": time.perf_counter(),
    }
    log_every = max(int(log_every_chunks), 1)

    def _on_task_complete(task, result):
        if not getattr(task, "task_type", "").startswith("insert_chunk"):
            return
        value = getattr(result, "value", None) or {}
        if isinstance(value, dict):
            row_count = int(value.get("row_count", 0) or 0)
        else:
            row_count = 0
        state["rows"] += row_count
        state["chunks"] += 1
        checkpoint = IngestCheckpoint(
            job_id=job_id,
            artifact_id=artifact_id,
            chunks_committed=state["chunks"],
            rows_committed=state["rows"],
            last_updated=utc_now_iso(),
            attempt_id=attempt_id,
            lease_generation=lease.generation if lease is not None else None,
        )
        try:
            checkpoint_started = time.perf_counter()
            if lease is not None:
                _refresh_ingest_lease(artifact_store, lease)
            artifact_store.write_checkpoint(
                job_id,
                artifact_id,
                checkpoint,
                attempt_id=attempt_id,
                lease_generation=lease.generation if lease is not None else None,
            )
            try:
                write_job_event(
                    artifact_store,
                    PipelineJobEvent(
                        event_type="checkpoint_progress",
                        job_id=job_id,
                        stage="ingest",
                        table_id=artifact_id,
                        attempt_id=attempt_id,
                        rows_committed=checkpoint.rows_committed,
                        chunks_committed=checkpoint.chunks_committed,
                        next_action="continue",
                        metadata={
                            "file_path": file_path,
                            "total_rows": total_rows,
                            "chunk_size": chunk_size,
                        },
                    ),
                )
            except Exception:
                logger.debug(
                    "[ingest] Failed to write checkpoint event job=%s artifact=%s",
                    job_id,
                    artifact_id,
                    exc_info=True,
                )
            checkpoint_write_ms = (time.perf_counter() - checkpoint_started) * 1000
        except Exception:
            checkpoint_write_ms = -1.0
            logger.warning(
                "[ingest] Failed to write checkpoint job=%s artifact=%s",
                job_id,
                artifact_id,
                exc_info=True,
            )
            raise

        should_log = (
            state["chunks"] == initial_chunks + 1
            or state["chunks"] % log_every == 0
            or (total_rows is not None and state["rows"] >= total_rows)
        )
        if should_log:
            now = time.perf_counter()
            elapsed_since_log = max(now - state["last_logged_at"], 1e-6)
            rows_since_log = max(state["rows"] - state["last_rows"], 0)
            rows_per_second = rows_since_log / elapsed_since_log
            remaining_rows = (
                max(total_rows - state["rows"], 0) if total_rows is not None else None
            )
            eta_seconds = (
                remaining_rows / rows_per_second
                if remaining_rows is not None and rows_per_second > 0
                else None
            )
            percent = (state["rows"] / total_rows) * 100 if total_rows else None
            logger.info(
                "[ingest][progress] job=%s file=%s table=%s chunks=%d rows=%d/%s "
                "chunk_size=%s pct=%s rows_per_s=%.1f checkpoint_ms=%.1f eta_s=%s",
                job_id,
                file_path or "-",
                artifact_id,
                state["chunks"],
                state["rows"],
                total_rows if total_rows is not None else "?",
                chunk_size if chunk_size is not None else "?",
                f"{percent:.1f}" if percent is not None else "?",
                rows_per_second,
                checkpoint_write_ms,
                f"{eta_seconds:.0f}" if eta_seconds is not None else "?",
            )
            state["last_logged_at"] = now
            state["last_rows"] = state["rows"]

        if is_cancelled and is_cancelled():
            raise PipelineCancelled(
                f"Job {job_id} cancelled during ingestion "
                f"(after {state['chunks']} chunks, {state['rows']} rows)",
            )

    return _on_task_complete


# ---------------------------------------------------------------------------
# Per-message UNIFY_KEY lifecycle
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _with_unify_key(binding: IngestBinding) -> AsyncIterator[str]:
    """Resolve + install ``UNIFY_KEY`` for the duration of one message.

    The shared worker pods do not carry a per-assistant ``UNIFY_KEY``
    in their environment. Instead, every message-processing code path
    enters this context manager, which:

    1. Looks up the caller's api_key from Orchestra via
       :func:`resolve_api_key` (cached per ``(user_id, assistant_id)``).
       The resolver reads ``SETTINGS.ORCHESTRA_URL`` and
       ``SETTINGS.ORCHESTRA_ADMIN_KEY`` itself; we do not re-plumb
       those here.
    2. Installs it as ``os.environ["UNIFY_KEY"]`` so every subsequent
       Unify SDK call -- including any deep inside
       :class:`DataManager` / :class:`FileManager` -- picks it up.
       The SDK contract is env-based (see
       ``unity.session_details.SessionDetails.unify_key`` which falls
       back to ``os.environ.get("UNIFY_KEY", "")`` on every read), so
       this ``os.environ`` write is load-bearing and cannot be
       replaced by a pydantic-settings update.
    3. On exit, restores the previous value (or deletes the variable
       if unset) so a leaked key never bleeds into heartbeat or
       shutdown code paths after the message completes.

    The worker is one-pod-one-message, so there is no risk of
    overlapping context managers mutating ``os.environ`` concurrently.
    """
    api_key = await resolve_api_key(binding)

    previous = os.environ.get("UNIFY_KEY")
    os.environ["UNIFY_KEY"] = api_key
    try:
        yield api_key
    finally:
        if previous is None:
            os.environ.pop("UNIFY_KEY", None)
        else:
            os.environ["UNIFY_KEY"] = previous


def _park_inflight_message(
    *,
    artifact_store: Any,
    run_id: str,
    item: ReceivedWorkItem,
    msg: IngestRequested,
) -> None:
    """Upload the raw payload of the in-flight message to the GCS parking lot.

    Called from the ``PipelineCancelled`` handler when the control
    watcher's ``pause_event`` fired.  The checkpoint is already
    durable in GCS (``_make_checkpoint_callback`` writes per chunk),
    so the parked payload is all the state the resume flow needs to
    re-spawn this work item.

    Failures are logged but not re-raised: the alternative (letting
    the Pub/Sub message redeliver) would put the message right back
    in the backlog and defeat the entire purpose of parking.  We'd
    rather proceed with an ack'd-but-unparked message and require the
    operator to re-dispatch than loop forever on a GCS write error.
    """
    from unity_deploy.infra.gcp.message_parking import park_message

    dispatch_id = msg.dispatch_id
    if not dispatch_id:
        logger.warning(
            "[ingest] Cannot park message %s for job=%s: no dispatch_id.",
            item.message_id,
            run_id,
        )
        return

    pod_name = os.environ.get("HOSTNAME") or "ingest-worker"
    try:
        park_message(
            artifact_store,
            dispatch_id=dispatch_id,
            published_at=item.published_at,
            message_id=item.message_id,
            payload=item.payload or {},
            topic="ingest",
            parked_by=f"ingest-worker:{pod_name}",
        )
    except Exception:
        logger.exception(
            "[ingest] Failed to park message %s for job=%s; "
            "checkpoint is still safe in GCS, but operator will need "
            "to re-dispatch this file to resume it.",
            item.message_id,
            run_id,
        )
        raise


def _is_terminal_job(job_store: Any, job_id: str) -> tuple[bool, str]:
    try:
        job = job_store.read_job(job_id)
    except Exception:
        return False, ""
    status = str(getattr(job, "status", "") or "")
    return status in _TERMINAL_JOB_STATUSES, status


def _mark_ingest_running(
    *,
    job_store: Any,
    artifact_store: Any,
    settings: Any,
    item: ReceivedWorkItem,
    msg: IngestRequested,
) -> None:
    """Best-effort job/event update once an ingest message is actually owned."""
    try:
        job = job_store.read_job(msg.job_id)
        previous_status = str(getattr(job, "status", "") or "")
        if previous_status not in {"success", "cancelled", "paused"}:
            job.status = "running"
            if previous_status != "running" or not job.started_at:
                job.started_at = utc_now_iso()
            job.finished_at = None
            job.error = None
            if msg.dispatch_id and not job.dispatch_id:
                job.dispatch_id = msg.dispatch_id
            job.metadata = {
                **(job.metadata or {}),
                "active_stage": "ingest",
                "active_message_id": (
                    (job.metadata or {}).get("active_message_id")
                    if previous_status == "running"
                    else item.pubsub_message_id or item.message_id
                ),
                "previous_status": (
                    previous_status
                    if previous_status and previous_status != "running"
                    else (job.metadata or {}).get("previous_status", "")
                ),
            }
            job_store.upsert_job(job)
    except Exception:
        logger.debug("Could not mark ingest job running for %s", msg.job_id)

    try:
        write_job_event(
            artifact_store,
            PipelineJobEvent(
                event_type="ingest_attempt_started",
                environment=getattr(settings, "environment", ""),
                project_id=getattr(settings.pubsub, "project_id", ""),
                job_id=msg.job_id,
                dispatch_id=msg.dispatch_id or "",
                stage="ingest",
                pubsub_message_id=item.pubsub_message_id or item.message_id,
                delivery_attempt=item.delivery_attempt,
                receipt_hash=make_receipt_hash(item.receipt_id),
                source_subscription=item.source_subscription,
                worker_pod=os.environ.get("HOSTNAME", ""),
                next_action="process_manifest",
            ),
        )
    except Exception:
        logger.debug("Could not write ingest_attempt_started event for %s", msg.job_id)


def _lease_key(job_id: str, table_id: str) -> str:
    safe_table = str(table_id or "table").replace("/", "_")
    return f"jobs/{job_id}/leases/ingest-{safe_table}.json"


def _new_owner_id(stage: str) -> str:
    pod = os.environ.get("HOSTNAME") or "unknown-pod"
    return f"{stage}:{pod}:{uuid.uuid4().hex[:12]}"


def _acquire_ingest_lease(
    artifact_store: Any,
    *,
    job_id: str,
    table_id: str,
    attempt_id: str,
) -> _ActiveIngestLease:
    owner_id = _new_owner_id("ingest")
    key = _lease_key(job_id, table_id)
    try:
        record: LeaseRecord = artifact_store.acquire_lease(
            key,
            owner_id=owner_id,
            attempt_id=attempt_id,
            stage="ingest",
            ttl_seconds=int(os.environ.get("UNITY_INGEST_ATTEMPT_LEASE_TTL", "900")),
        )
    except LeaseNotAcquired as exc:
        lease = exc.lease
        logger.info(
            "[ingest] Duplicate live attempt for job=%s table=%s owner=%s "
            "expires_at=%s; acking duplicate message",
            job_id,
            table_id,
            lease.owner_id if lease else "?",
            lease.expires_at if lease else "?",
        )
        raise DuplicateLiveAttempt(
            str(exc),
            stage="ingest",
            lease=lease,
        ) from exc
    logger.info(
        "[ingest] Acquired attempt lease job=%s table=%s owner=%s attempt=%s "
        "generation=%s",
        job_id,
        table_id,
        record.owner_id,
        record.attempt_id,
        record.generation,
    )
    return _ActiveIngestLease(
        key=key,
        owner_id=owner_id,
        attempt_id=attempt_id,
        generation=record.generation,
    )


def _refresh_ingest_lease(artifact_store: Any, lease: _ActiveIngestLease) -> None:
    try:
        renewed = artifact_store.refresh_lease(
            lease.key,
            owner_id=lease.owner_id,
            attempt_id=lease.attempt_id,
            generation=lease.generation,
            ttl_seconds=int(os.environ.get("UNITY_INGEST_ATTEMPT_LEASE_TTL", "900")),
        )
    except StaleLeaseError:
        raise
    lease.generation = renewed.generation


def _is_retryable_lease_error(error: str | None) -> bool:
    text = str(error or "")
    return text.startswith("Lease ") and (
        " is owned by " in text
        or " owner changed " in text
        or " generation changed " in text
    )


async def handle_ingest_message(
    item: ReceivedWorkItem,
    *,
    infra: WorkerInfra,
    ack_receipt: Callable[[], Awaitable[None]] | None = None,
) -> bool:
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

    is_terminal, terminal_status = _is_terminal_job(job_store, run_id)
    if is_terminal:
        logger.info(
            "[ingest] Acking duplicate terminal job=%s status=%s without work",
            run_id,
            terminal_status,
        )
        if ack_receipt is not None:
            await ack_receipt()
            run_ledger.close()
            cost_ledger.close()
            return True
        run_ledger.close()
        cost_ledger.close()
        return False

    logger.info(
        "[ingest] Starting job=%s, manifest=%s, mode=%s",
        run_id,
        msg.manifest_key,
        msg.ingestion_mode,
    )
    ingest_start = time.perf_counter()

    overall_error: str | None = None
    total_rows = 0
    file_path: str = ""
    scratch_dir_ctx = tempfile.TemporaryDirectory(prefix=f"ingest_{run_id}_")
    scratch_dir = Path(scratch_dir_ctx.name)
    acked = False

    # Start the control watcher before any heavy work so a pause/cancel
    # issued while the manifest download is running is still detected
    # at the next hot-loop boundary.  The watcher is stopped in the
    # outer ``finally`` below.
    watch = _spawn_control_watcher(artifact_store, run_id)
    try:
        _mark_ingest_running(
            job_store=job_store,
            artifact_store=artifact_store,
            settings=infra.settings,
            item=item,
            msg=msg,
        )
        manifest_payload: dict = artifact_store.get_json(msg.manifest_key)
        plan = IngestPlan.model_validate(manifest_payload)
        # Stage any remote (gs://) artifact handles to a per-message scratch
        # directory. row_streaming iterates rows against the local path, so
        # remote URIs must be materialised before fm_process_plan /
        # ingest_artifacts are invoked. The scratch dir is cleaned up in the
        # finally block below.
        plan = _stage_remote_handles(
            plan,
            artifact_store=artifact_store,
            scratch_dir=scratch_dir,
        )
        _guard_scratch_usage(
            scratch_dir=scratch_dir,
            run_id=run_id,
            phase="after_staging",
        )
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

        check_cancelled: CancellationCheck = watch.is_cancelled

        if overall_error is None:
            if msg.ingestion_mode == "fm":
                total_rows, overall_error = await _run_fm_mode(
                    plan=plan,
                    msg=msg,
                    infra=infra,
                    run_ledger=run_ledger,
                    is_cancelled=check_cancelled,
                )
            else:
                total_rows, overall_error = await _run_dm_mode(
                    plan=plan,
                    msg=msg,
                    infra=infra,
                    run_ledger=run_ledger,
                    is_cancelled=check_cancelled,
                )
            _delete_staged_scratch_files(scratch_dir, run_id=run_id)
            _guard_scratch_usage(
                scratch_dir=scratch_dir,
                run_id=run_id,
                phase="after_durable_ingest",
            )

        if overall_error is None and ack_receipt is not None:
            await ack_receipt()
            acked = True
            logger.info("[ingest] Acked job=%s after durable ingest", run_id)

        try:
            job = job_store.read_job(run_id)
            if msg.dispatch_id and not job.dispatch_id:
                job.dispatch_id = msg.dispatch_id
            if job.status not in ("cancelled", "paused"):
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

    except DuplicateLiveAttempt as exc:
        lease = exc.lease
        run_ledger.write(
            PipelineStageManifest(
                run_id=run_id,
                file_path=file_path,
                stage_name="ingest",
                status="success",
                duration_ms=(time.perf_counter() - ingest_start) * 1000,
                meta={
                    "duplicate_live_attempt": True,
                    "active_owner": getattr(lease, "owner_id", ""),
                    "active_expires_at": getattr(lease, "expires_at", ""),
                },
            ),
        )
        try:
            write_job_event(
                artifact_store,
                PipelineJobEvent(
                    event_type="duplicate_live_attempt_acked",
                    environment=getattr(infra.settings, "environment", ""),
                    project_id=getattr(infra.settings.pubsub, "project_id", ""),
                    job_id=run_id,
                    dispatch_id=msg.dispatch_id or "",
                    stage="ingest",
                    pubsub_message_id=item.pubsub_message_id or item.message_id,
                    delivery_attempt=item.delivery_attempt,
                    receipt_hash=make_receipt_hash(item.receipt_id),
                    source_subscription=item.source_subscription,
                    worker_pod=os.environ.get("HOSTNAME", ""),
                    error_message=str(exc),
                    next_action="ack_duplicate",
                    metadata={
                        "active_owner": getattr(lease, "owner_id", ""),
                        "active_expires_at": getattr(lease, "expires_at", ""),
                    },
                ),
            )
        except Exception:
            logger.debug(
                "[ingest] Failed to write duplicate ack event job=%s",
                run_id,
                exc_info=True,
            )
        if ack_receipt is not None:
            await ack_receipt()
            acked = True
        logger.info("[ingest] Acked duplicate live attempt for job=%s", run_id)
        return acked
    except PipelineCancelled:
        if watch.paused():
            # Pause path: park the in-flight payload, leave the job's
            # status=paused (operator already wrote that) and the
            # checkpoint intact, and return normally so the entrypoint
            # acks the Pub/Sub message.  This collapses backlog so the
            # HPA can deprovision, yet preserves progress so ``resume``
            # picks up where we left off via ``read_checkpoint``.
            _park_inflight_message(
                artifact_store=artifact_store,
                run_id=run_id,
                item=item,
                msg=msg,
            )
            run_ledger.write(
                PipelineStageManifest(
                    run_id=run_id,
                    file_path=file_path,
                    stage_name="ingest",
                    status="error",
                    duration_ms=(time.perf_counter() - ingest_start) * 1000,
                    error="paused",
                    meta={"paused_message_id": item.message_id},
                ),
            )
            logger.info(
                "[ingest] Job %s paused mid-flight after %.1fs; "
                "parked message=%s (dispatch=%s)",
                run_id,
                time.perf_counter() - ingest_start,
                item.message_id,
                msg.dispatch_id or "-",
            )
            # Intentionally skip attachment_callback: the attachment is
            # NOT complete yet. It fires when resume's final message
            # finishes successfully.
            return False

        overall_error = "cancelled"
        logger.info(
            "[ingest] Job %s cancelled mid-flight after %.1fs",
            run_id,
            time.perf_counter() - ingest_start,
        )
        try:
            job = job_store.read_job(run_id)
            if job.status != "cancelled":
                job.status = "cancelled"
                job.finished_at = utc_now_iso()
            job_store.upsert_job(job)
        except Exception:
            logger.debug("Could not update job status for %s", run_id)
        if msg.attachment_callback is not None:
            await _publish_attachment_completion(
                callback=msg.attachment_callback,
                success=False,
                error="cancelled",
            )
    except RetryWorkItem:
        raise
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
        if acked:
            logger.warning(
                "[ingest] Finalize failed after durable ack for job=%s; "
                "rows remain committed and Pub/Sub will not redeliver.",
                run_id,
            )
            return True
        raise
    finally:
        # Stop the watcher before closing ledgers so any log message it
        # emits still has the run-scoped ledgers available.  ``stop()``
        # is idempotent and safe to call even if the task already
        # self-terminated on a status flip.
        try:
            await watch.stop()
        except Exception:
            logger.debug("[ingest] control watcher stop failed for job=%s", run_id)
        run_ledger.close()
        cost_ledger.close()
        scratch_dir_ctx.cleanup()
    return acked


# ---------------------------------------------------------------------------
# FM mode
# ---------------------------------------------------------------------------


async def _run_fm_mode(
    *,
    plan: IngestPlan,
    msg: IngestRequested,
    infra: WorkerInfra,
    run_ledger,
    is_cancelled: CancellationCheck | None = None,
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

    async with _with_unify_key(fm_binding):
        return await _run_fm_mode_inner(
            plan=plan,
            msg=msg,
            infra=infra,
            run_ledger=run_ledger,
            fm_binding=fm_binding,
            activate_unify_context=activate_unify_context,
            is_cancelled=is_cancelled,
        )


async def _run_fm_mode_inner(
    *,
    plan: IngestPlan,
    msg: IngestRequested,
    infra: WorkerInfra,
    run_ledger,
    fm_binding,
    activate_unify_context,
    is_cancelled: CancellationCheck | None = None,
) -> tuple[int, str | None]:
    """Body of FM dispatch, run inside the per-message UNIFY_KEY scope."""
    from unity.data_manager import DataManager
    from unity.file_manager.filesystem_adapters.local_adapter import (
        LocalFileSystemAdapter,
    )
    from unity.file_manager.managers.file_manager import FileManager
    from unity.file_manager.managers.utils.executor import fm_process_plan

    activate_unify_context(
        user_id=fm_binding.user_id,
        assistant_id=fm_binding.assistant_id,
        managers=[FileManager, DataManager],
    )

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

    config = _build_fm_config_from_plan(plan)
    instrumentation = PipelineInstrumentation.from_config(
        config,
        run_id=msg.job_id,
        file_count=1,
    )

    total_rows = 0
    error: str | None = None
    retryable_lease_error: str | None = None
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
                storage_client=infra.storage_client,
                artifact_store=infra.artifact_store,
                job_id=msg.job_id,
                is_cancelled=is_cancelled,
            )
            status = str(getattr(result, "status", "error") or "error")
            if status != "success":
                error = str(getattr(result, "error", "fm_process_plan failed") or "")
            total_rows = _extract_total_rows(result)
    except PipelineCancelled:
        raise
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


def _build_fm_config_from_plan(plan: IngestPlan):
    """Build a ``FilePipelineConfig`` from ``TableMeta`` config fields.

    Synthesises ``EmbeddingsConfig`` and ``BusinessContextsConfig``
    objects so the existing FM executor pipeline (which resolves embed
    columns and descriptions via config lookups) picks them up without
    any changes to the FM internals.
    """
    from unity.file_manager.types.config import (
        BusinessContextsConfig,
        EmbeddingsConfig,
        FileBusinessContextSpec,
        FileEmbeddingSpec,
        FilePipelineConfig,
        IngestConfig,
        TableBusinessContextSpec,
        TableEmbeddingSpec,
    )

    embed_specs: list[TableEmbeddingSpec] = []
    table_bc_specs: list[TableBusinessContextSpec] = []
    embed_strategy = "off"

    for meta in plan.tables_meta:
        label = meta.label or meta.sheet_name or meta.table_id

        if meta.embed_columns:
            embed_specs.append(
                TableEmbeddingSpec(
                    table=label,
                    source_columns=list(meta.embed_columns),
                    target_columns=[f"{c}_embed" for c in meta.embed_columns],
                ),
            )
            if meta.embed_strategy and meta.embed_strategy != "off":
                embed_strategy = meta.embed_strategy

        if meta.column_descriptions or meta.description:
            table_bc_specs.append(
                TableBusinessContextSpec(
                    table=label,
                    table_description=meta.description,
                    column_descriptions=dict(meta.column_descriptions or {}),
                ),
            )

    file_path = plan.file_path
    embed_config = EmbeddingsConfig(strategy=embed_strategy)
    if embed_specs:
        embed_config = EmbeddingsConfig(
            strategy=embed_strategy,
            file_specs=[
                FileEmbeddingSpec(
                    file_path=file_path,
                    context="per_file_table",
                    tables=embed_specs,
                ),
            ],
        )

    business_contexts = None
    if table_bc_specs:
        business_contexts = BusinessContextsConfig(
            file_contexts=[
                FileBusinessContextSpec(
                    file_path=file_path,
                    table_contexts=table_bc_specs,
                ),
            ],
        )

    return FilePipelineConfig(
        embed=embed_config,
        ingest=IngestConfig(business_contexts=business_contexts),
    )


# ---------------------------------------------------------------------------
# DM mode
# ---------------------------------------------------------------------------


async def _run_dm_mode(
    *,
    plan: IngestPlan,
    msg: IngestRequested,
    infra: WorkerInfra,
    run_ledger,
    is_cancelled: CancellationCheck | None = None,
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
    if dm_binding is None:
        raise RuntimeError(
            "DM ingest mode requires msg.dm_binding to be set so the worker "
            "can resolve the Unify api_key for the dispatching user.",
        )
    default_target = dm_binding.target_context or msg.target_context or msg.job_id

    from .worker_utils import activate_unify_context

    async with _with_unify_key(dm_binding):
        return await _run_dm_mode_inner(
            plan=plan,
            msg=msg,
            infra=infra,
            run_ledger=run_ledger,
            dm_binding=dm_binding,
            default_target=default_target,
            activate_unify_context=activate_unify_context,
            is_cancelled=is_cancelled,
        )


async def _run_dm_mode_inner(
    *,
    plan: IngestPlan,
    msg: IngestRequested,
    infra: WorkerInfra,
    run_ledger,
    dm_binding,
    default_target: str,
    activate_unify_context,
    is_cancelled: CancellationCheck | None = None,
) -> tuple[int, str | None]:
    """Body of DM dispatch, run inside the per-message UNIFY_KEY scope."""
    from unity.data_manager import DataManager
    from unity.file_manager.types.config import FilePipelineConfig

    # DM dispatches are assistant-scoped too: they still ingest into an
    # explicit DataManager context, but Orchestra key resolution and
    # Unify activation should happen against the bound assistant rather
    # than a synthetic placeholder.
    activate_unify_context(
        user_id=dm_binding.user_id,
        assistant_id=dm_binding.assistant_id,
        managers=[DataManager],
    )

    dm = DataManager()
    config = FilePipelineConfig()
    instrumentation = PipelineInstrumentation.from_config(
        config,
        run_id=msg.job_id,
        file_count=1,
    )

    artifact_store = infra.artifact_store
    gcs_client = infra.storage_client
    attempt_id = uuid.uuid4().hex

    work_items: list[ArtifactWorkItem] = []
    missing_inputs: list[str] = []
    for meta in plan.tables_meta:
        table_id = str(meta.table_id or "")
        handle = (plan.table_inputs or {}).get(table_id)
        if handle is None:
            missing_inputs.append(table_id or str(meta.label or "unknown"))
            continue
        columns = list(meta.columns or []) or list(
            getattr(handle, "columns", []) or [],
        )
        declared_row_count = (
            meta.row_count
            if meta.row_count is not None
            else getattr(handle, "row_count", None)
        )
        if declared_row_count is None:
            raise RuntimeError(
                f"Ingest manifest table={table_id} has no declared row_count; "
                "strict row-count validation requires parser counts.",
            )
        row_count = int(declared_row_count)
        target_context = meta.context or default_target

        fields = (
            {
                name: {"description": desc}
                for name, desc in meta.column_descriptions.items()
            }
            if meta.column_descriptions
            else None
        )
        fields = dict(fields or {})
        fields.setdefault(_PRIVATE_INGEST_KEY, "str")
        unique_keys = {_PRIVATE_INGEST_KEY: "str"}

        post_ingest_config = None
        if meta.post_ingest:
            from unity.data_manager.types.ingest import PostIngestConfig

            post_ingest_config = PostIngestConfig.model_validate(meta.post_ingest)

        ckpt = artifact_store.read_checkpoint(msg.job_id, table_id)
        skip_rows = ckpt.rows_committed if ckpt else 0
        initial_chunks = ckpt.chunks_committed if ckpt else 0
        if ckpt:
            logger.info(
                "[ingest][dm] Resuming table=%s from checkpoint: "
                "%d rows, %d chunks already committed",
                table_id,
                skip_rows,
                initial_chunks,
            )
        work_items.append(
            ArtifactWorkItem(
                kind="table",
                label=str(meta.label or table_id or "table"),
                stage_name="ingest_table",
                payload={
                    "dm": dm,
                    "context": target_context,
                    "handle": handle,
                    "batch_size": meta.chunk_size or msg.batch_size,
                    "description": meta.description,
                    "fields": fields,
                    "unique_keys": unique_keys,
                    "embed_columns": meta.embed_columns,
                    "embed_strategy": meta.embed_strategy,
                    "post_ingest": post_ingest_config,
                    "skip_rows": skip_rows,
                    "initial_chunks": initial_chunks,
                    "table_id": table_id,
                    "storage_client": gcs_client,
                    "attempt_id": attempt_id,
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
    if missing_inputs:
        raise RuntimeError(
            f"Ingest manifest missing table input handles for {missing_inputs}; "
            "refusing partial ingest.",
        )
    if not work_items and plan.tables_meta:
        raise RuntimeError("Ingest manifest did not produce any table work items.")

    def _dm_ingest_fn(item: ArtifactWorkItem) -> dict:
        pl = item.payload
        lease = _acquire_ingest_lease(
            artifact_store,
            job_id=msg.job_id,
            table_id=pl.get("table_id", ""),
            attempt_id=pl.get("attempt_id", ""),
        )
        pl["lease"] = lease

        def _before_insert_chunk(**_kwargs) -> None:
            if is_cancelled and is_cancelled():
                raise PipelineCancelled(
                    f"Job {msg.job_id} cancelled before next DM chunk",
                )
            _refresh_ingest_lease(artifact_store, lease)

        on_complete = _make_checkpoint_callback(
            artifact_store,
            msg.job_id,
            pl.get("table_id", ""),
            attempt_id=pl.get("attempt_id", ""),
            lease=lease,
            initial_rows=pl.get("skip_rows", 0),
            initial_chunks=pl.get("initial_chunks", 0),
            total_rows=item.row_count,
            file_path=plan.file_path,
            chunk_size=pl["batch_size"],
            is_cancelled=is_cancelled,
        )
        ingest_started = time.perf_counter()
        logger.info(
            "[ingest][dm] Starting table=%s file=%s total_rows=%s "
            "chunk_size=%s skip_rows=%s initial_chunks=%s",
            pl.get("table_id", ""),
            plan.file_path,
            item.row_count if item.row_count is not None else "?",
            pl["batch_size"],
            pl.get("skip_rows", 0),
            pl.get("initial_chunks", 0),
        )
        result = pl["dm"].ingest(
            pl["context"],
            None,
            table_input_handle=pl["handle"],
            chunk_size=pl["batch_size"],
            description=pl.get("description"),
            fields=pl.get("fields"),
            unique_keys=pl.get("unique_keys"),
            embed_columns=pl.get("embed_columns"),
            embed_strategy=pl.get("embed_strategy", "off"),
            post_ingest=pl.get("post_ingest"),
            on_task_complete=on_complete,
            storage_client=pl.get("storage_client"),
            skip_rows=pl.get("skip_rows", 0),
            expected_total_rows=item.row_count if item.row_count is not None else None,
            private_ingest_key_column=_PRIVATE_INGEST_KEY,
            private_ingest_key_prefix=(
                f"{msg.dispatch_id or 'dispatch'}:{msg.job_id}:"
                f"{pl.get('table_id', '')}"
            ),
            before_insert_chunk=_before_insert_chunk,
        )
        elapsed = time.perf_counter() - ingest_started
        rows_inserted = int(getattr(result, "rows_inserted", 0) or 0)
        logger.info(
            "[ingest][dm] Finished table=%s file=%s inserted_rows=%d "
            "elapsed_s=%.1f rows_per_s=%.1f",
            pl.get("table_id", ""),
            plan.file_path,
            rows_inserted,
            elapsed,
            rows_inserted / elapsed if elapsed > 0 else 0.0,
        )
        return {
            "ingest_result": result,
            "context": pl["context"],
            "row_count": rows_inserted,
        }

    total_rows = 0
    error: str | None = None
    retryable_lease_error: str | None = None
    start = time.perf_counter()
    try:
        with instrumentation:
            artifact_results = ingest_artifacts(
                work_items=work_items,
                ingest_fn=_dm_ingest_fn,
                instrumentation=instrumentation,
                source_path=plan.file_path,
                max_workers=1,
                retry_config=config.retry,
                is_cancelled=is_cancelled,
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
            if retryable_lease_error is None and _is_retryable_lease_error(ar.error):
                retryable_lease_error = ar.error
    except PipelineCancelled:
        raise
    except DuplicateLiveAttempt:
        raise
    except Exception as exc:
        error = str(exc) or "ingest_artifacts raised"
        logger.exception("[ingest][dm] Failed for %s", plan.file_path)

    if retryable_lease_error is not None:
        raise DuplicateLiveAttempt(
            retryable_lease_error,
            stage="ingest",
        )

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
