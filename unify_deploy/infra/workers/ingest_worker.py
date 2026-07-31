"""Ingest worker: consumes IngestRequested messages, streams rows into DataManager.

The worker consumes a pointer-only :class:`IngestPlan` that the parse
worker has already materialised to GCS.  Heavy data (content rows,
table row bodies) lives behind handles, so the manifest this worker
downloads is always KB-scale regardless of the source file size.

Two ingestion flavours are supported, driven by
``IngestRequested.ingestion_mode``:

- **FM mode** (``ingestion_mode="fm"``): activates a Unify context
  derived from ``msg.fm_binding``, instantiates a
  :class:`unify.file_manager.managers.file_manager.FileManager`, and
  delegates the work to
  :func:`unify.file_manager.managers.utils.executor.fm_process_plan`.
  The resulting rows land under ``Files/{alias}/{storage_id}/...`` with
  a proper ``FileRecords`` entry.

- **DM mode** (``ingestion_mode="dm"``): drives
  :func:`unify.common.pipeline.ingest_artifacts` directly over the
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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from unify.common.pipeline import (
    CancellationCheck,
    CheckpointedIngest,
    DuplicateLiveAttempt,
    IngestPlan,
    NullJournal,
    PipelineCancelled,
    PipelineInstrumentation,
    RunJournal,
    TableWork,
    incomplete_tables,
    journal_from_payload,
)
from unify.ingestion_manager.settings import IngestionSettings
from unify.common.pipeline._utils import utc_now_iso
from unify.common.pipeline.run_ledger import PipelineStageManifest
from unify.common.pipeline.types import (
    AttachmentCallback,
    CsvFileHandle,
    IngestBinding,
    IngestRequested,
    ObjectStoreArtifactHandle,
    TableInputHandle,
    XlsxSheetHandle,
)
from unify.common.pipeline.work_queue import ReceivedWorkItem, RetryWorkItem
from unify.common.context_registry import ContextRegistry
from unify.session_details import SESSION_DETAILS

from .assistant_key_resolver import ResolvedAssistant, resolve_assistant
from unify_deploy.infra.gcp.pipeline_observability import (
    PipelineJobEvent,
    make_receipt_hash,
    write_job_event,
)
from .worker_utils import is_shutdown_requested

if TYPE_CHECKING:
    from .worker_utils import WorkerInfra

logger = logging.getLogger(__name__)

_TERMINAL_JOB_STATUSES = {"success", "error", "cancelled"}
_PRIVATE_INGEST_KEY = "_unity_ingest_key"

# Distinctive marker embedded in the surrender RetryWorkItem message. The
# per-chunk hook raises RetryWorkItem, but ingest_artifacts' run_with_retry
# captures it (it is an ``Exception``) into the work-result error string rather
# than propagating it. _run_dm_mode_inner detects this sentinel and re-raises
# RetryWorkItem so the entrypoint nacks for immediate redelivery (mirrors the
# retryable-lease-error -> DuplicateLiveAttempt re-raise).
_SURRENDER_SENTINEL = "surrendering in-flight chunk"


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


# ---------------------------------------------------------------------------
# Per-message UNIFY_KEY lifecycle
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _with_unify_key(binding: IngestBinding) -> AsyncIterator[ResolvedAssistant]:
    """Resolve + install per-message identity for the duration of one message.

    The shared worker pods do not carry a per-assistant ``UNIFY_KEY``
    in their environment. Instead, every message-processing code path
    enters this context manager, which:

    1. Looks up the caller via :func:`resolve_assistant` (cached per
       ``(user_id, assistant_id)``), yielding the Unify api_key plus the
       assistant's live ``team_ids`` / ``team_summaries``. The resolver
       reads ``SETTINGS.ORCHESTRA_URL`` and ``SETTINGS.ORCHESTRA_ADMIN_KEY``
       itself; we do not re-plumb those here.
    2. Installs the api_key as ``os.environ["UNIFY_KEY"]`` so every
       subsequent Unify SDK call -- including any deep inside
       :class:`DataManager` / :class:`FileManager` -- picks it up.
       The SDK contract is env-based (see
       ``unify.session_details.SessionDetails.unify_key`` which falls
       back to ``os.environ.get("UNIFY_KEY", "")`` on every read), so
       this ``os.environ`` write is load-bearing and cannot be
       replaced by a pydantic-settings update.
    3. Hydrates ``SESSION_DETAILS`` with the dispatching assistant's
       agent_id and team memberships so shared-scoped writes route to
       the right ``Data`` root and stamp authorship against the real
       author rather than ``None``.
    4. On exit, restores the previous api_key and session identity (or
       clears them if unset) so a message's identity never bleeds into
       the next message or into heartbeat / shutdown code paths.

    The worker is one-pod-one-message, so there is no risk of
    overlapping context managers mutating ``os.environ`` or
    ``SESSION_DETAILS`` concurrently.
    """
    resolved = await resolve_assistant(binding)

    previous_key = os.environ.get("UNIFY_KEY")
    os.environ["UNIFY_KEY"] = resolved.api_key

    previous_agent_id = SESSION_DETAILS.assistant.agent_id
    previous_team_ids = SESSION_DETAILS.team_ids
    previous_team_summaries = SESSION_DETAILS.team_summaries
    SESSION_DETAILS.assistant.agent_id = int(binding.assistant_id)
    SESSION_DETAILS.team_ids = list(resolved.team_ids)
    SESSION_DETAILS.team_summaries = list(resolved.team_summaries)
    try:
        yield resolved
    finally:
        if previous_key is None:
            os.environ.pop("UNIFY_KEY", None)
        else:
            os.environ["UNIFY_KEY"] = previous_key
        SESSION_DETAILS.assistant.agent_id = previous_agent_id
        SESSION_DETAILS.team_ids = previous_team_ids
        SESSION_DETAILS.team_summaries = previous_team_summaries


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
    from unify_deploy.infra.gcp.message_parking import park_message

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


def _enforce_ingest_completion(
    *,
    plan: IngestPlan,
    artifact_store: Any,
    job_store: Any,
    infra: WorkerInfra,
    msg: IngestRequested,
    run_id: str,
    item: ReceivedWorkItem,
) -> None:
    """Finalization correctness gate (Phase 0).

    Refuse to let a DM ingest finalize as ``success`` while any table's
    durable checkpoint is short of the parser-declared ``row_count``.
    Records the discrepancy, emits a ``job_incomplete`` event, and either
    nacks for a fresh resume (``RetryWorkItem``) or, once the bounded retry
    budget is exhausted, raises to dead-letter the job so the shortfall
    surfaces instead of disappearing as a silent under-ingest.

    The job is kept non-terminal across resume attempts on purpose: a
    terminal status would be acked without work by the duplicate-terminal
    fast path at the top of :func:`handle_ingest_message`.

    The shortfall itself is computed by the shared engine, so the fleet and
    in-process execution measure completeness the same way. What is specific
    here is the bounded resume-then-dead-letter policy, which needs the queue.
    """
    shorts = incomplete_tables(
        _table_work_from_plan(plan, msg=msg, default_target=msg.target_context),
        artifact_store=artifact_store,
        job_id=run_id,
    )
    if not shorts:
        return

    detail = "; ".join(
        f"{t.table_id}: {t.rows_committed}/{t.declared_rows}" for t in shorts
    )
    settings = IngestionSettings()
    max_retries = settings.INCOMPLETE_MAX_RETRIES
    retry_delay = settings.INCOMPLETE_RETRY_SECONDS

    attempt = 0
    exhausted = False
    try:
        job = job_store.read_job(run_id)
        attempt = int((job.metadata or {}).get("incomplete_retry_attempt", 0)) + 1
        exhausted = attempt > max_retries
        job.metadata = {
            **(job.metadata or {}),
            "incomplete_retry_attempt": attempt,
            "incomplete_detail": detail,
            "rows_expected": sum(t.declared_rows for t in shorts),
            "rows_committed_short": sum(t.rows_committed for t in shorts),
        }
        if exhausted and job.status not in ("cancelled", "paused"):
            # Bounded backstop: stop resuming and surface the shortfall.
            job.status = "error"
            job.error = (
                f"ingest incomplete after {attempt - 1} resume attempts: {detail}"
            )
            job.finished_at = utc_now_iso()
        job_store.upsert_job(job)
    except Exception:
        logger.debug(
            "[ingest] Could not persist incomplete state for %s",
            run_id,
            exc_info=True,
        )

    try:
        write_job_event(
            artifact_store,
            PipelineJobEvent(
                event_type="job_incomplete",
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
                error_message=detail,
                next_action="dead_letter" if exhausted else "nack",
                metadata={
                    "incomplete_retry_attempt": attempt,
                    "max_retries": max_retries,
                },
            ),
        )
    except Exception:
        logger.debug(
            "[ingest] Failed to write job_incomplete event for %s",
            run_id,
            exc_info=True,
        )

    if exhausted:
        logger.error(
            "[ingest] Job=%s still incomplete after %d resume attempts (%s); "
            "dead-lettering instead of finalizing success.",
            run_id,
            attempt - 1,
            detail,
        )
        raise RuntimeError(
            f"ingest incomplete after {attempt - 1} resume attempts: {detail}",
        )

    logger.warning(
        "[ingest] Job=%s finalize blocked: checkpoint short of declared rows "
        "(%s); nacking to resume from checkpoint (attempt %d/%d).",
        run_id,
        detail,
        attempt,
        max_retries,
    )
    raise RetryWorkItem(
        f"ingest incomplete; resuming from checkpoint: {detail}",
        delay_seconds=float(retry_delay),
    )


async def handle_ingest_message(
    item: ReceivedWorkItem,
    *,
    infra: WorkerInfra,
    ack_receipt: Callable[[], Awaitable[None]] | None = None,
    should_surrender: Callable[[], bool] | None = None,
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
    msg = IngestRequested.model_validate(item.payload)
    run_id = msg.job_id

    artifact_store = infra.artifact_store
    work_queue = infra.work_queue
    job_store = infra.job_store
    run_ledger = infra.run_ledger_factory(run_id)
    # Events land in the run's own Ingestion contexts, written as the owning
    # assistant inside the per-message key scope. Append-only on purpose: the
    # run *row* is reconciled by the manager at read time from the control
    # plane's aggregate, because several jobs share one run and concurrent
    # workers doing read-modify-write on one row is exactly how two stores
    # start disagreeing.
    journal = journal_from_payload(item.payload)

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
            return True
        run_ledger.close()
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
                    journal=journal,
                )
            else:
                total_rows, overall_error = await _run_dm_mode(
                    plan=plan,
                    msg=msg,
                    infra=infra,
                    run_ledger=run_ledger,
                    is_cancelled=check_cancelled,
                    should_surrender=should_surrender,
                    journal=journal,
                )
            _delete_staged_scratch_files(scratch_dir, run_id=run_id)
            _guard_scratch_usage(
                scratch_dir=scratch_dir,
                run_id=run_id,
                phase="after_durable_ingest",
            )

            # Finalization correctness gate (Phase 0): never finalize a DM
            # ingest as success while any table's durable checkpoint is short
            # of the parser-declared row_count. Raises RetryWorkItem to resume
            # from checkpoint, or dead-letters once the retry budget is spent.
            if msg.ingestion_mode != "fm":
                _enforce_ingest_completion(
                    plan=plan,
                    artifact_store=artifact_store,
                    job_store=job_store,
                    infra=infra,
                    msg=msg,
                    run_id=run_id,
                    item=item,
                )

        try:
            job = job_store.read_job(run_id)
            if msg.dispatch_id and not job.dispatch_id:
                job.dispatch_id = msg.dispatch_id
            if job.status not in ("cancelled", "paused"):
                job.status = "success" if overall_error is None else "error"
                job.finished_at = utc_now_iso()
                job.error = overall_error
                metadata_updates: dict[str, Any] = {
                    "total_rows_inserted": total_rows,
                    "finalized_before_ack": overall_error is None,
                }
                if msg.ingestion_mode != "fm":
                    declared = sum(
                        entry.declared_rows
                        for entry in _table_work_from_plan(
                            plan,
                            msg=msg,
                            default_target=msg.target_context,
                        )
                    )
                    if declared:
                        metadata_updates["rows_expected"] = declared
                job.metadata = {
                    **(job.metadata or {}),
                    **metadata_updates,
                }
            job_store.upsert_job(job)
        except Exception:
            if overall_error is None:
                logger.exception(
                    "[ingest] Success finalization failed before ack for job=%s",
                    run_id,
                )
                raise
            logger.debug("Could not update error status for %s", run_id, exc_info=True)

        if overall_error is None and ack_receipt is not None:
            await ack_receipt()
            acked = True
            logger.info("[ingest] Acked job=%s after success finalization", run_id)

        run_ledger.flush()

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
                status="error",
                duration_ms=(time.perf_counter() - ingest_start) * 1000,
                error=str(exc),
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
                    event_type="duplicate_live_attempt_deferred",
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
                    next_action="defer_duplicate",
                    metadata={
                        "active_owner": getattr(lease, "owner_id", ""),
                        "active_expires_at": getattr(lease, "expires_at", ""),
                    },
                ),
            )
        except Exception:
            logger.debug(
                "[ingest] Failed to write duplicate deferral event job=%s",
                run_id,
                exc_info=True,
            )
        logger.info("[ingest] Deferring duplicate live attempt for job=%s", run_id)
        raise
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
                "[ingest] Post-ack completion side effect failed for job=%s; "
                "job was already finalized and Pub/Sub will not redeliver.",
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
        # No lease backstop here: CheckpointedIngest releases every lease it
        # holds in its own ``finally``, including when an exception skips the
        # per-table path, so an orphaned lease can no longer stall a survivor.
        run_ledger.close()
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
    journal: RunJournal | NullJournal = NullJournal(),
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
            journal=journal,
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
    journal: RunJournal | NullJournal = NullJournal(),
) -> tuple[int, str | None]:
    """Body of FM dispatch, run inside the per-message UNIFY_KEY scope."""
    from unify.data_manager import DataManager
    from unify.file_manager.filesystem_adapters.local_adapter import (
        LocalFileSystemAdapter,
    )
    from unify.file_manager.managers.file_manager import FileManager
    from unify.file_manager.managers.utils.executor import fm_process_plan

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
    start = time.perf_counter()
    journal.event(
        stage="ingest",
        state="running",
        message=f"Storing {plan.file_path} as documents on the worker fleet.",
    )
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

    journal.event(
        stage="ingest",
        state="succeeded" if error is None else "failed",
        level="info" if error is None else "error",
        done=total_rows,
        message=(
            f"Stored {plan.file_path} ({total_rows} row(s))."
            if error is None
            else f"{plan.file_path}: {error}"
        ),
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
    from unify.file_manager.types.config import (
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


def _validate_team_destination(dm_binding) -> None:
    """Fail fast when a team destination targets a non-member team.

    ``ContextRegistry.write_root`` also enforces membership when the
    write actually runs; this surfaces a clearer, earlier error (so the
    job lands in the DLQ with an actionable message) before any tables
    are provisioned. Reads ``SESSION_DETAILS.team_ids``, which the
    enclosing :func:`_with_unify_key` scope has already hydrated from the
    resolved assistant's live memberships.
    """
    canonical = ContextRegistry.canonical_destination(dm_binding.destination)
    if canonical is None:
        return
    team_id = int(canonical.split(":", 1)[1])
    if team_id not in SESSION_DETAILS.team_ids:
        raise RuntimeError(
            f"DM dispatch targets destination={dm_binding.destination!r} but "
            f"assistant_id={dm_binding.assistant_id} is not a live member of "
            f"team {team_id} (memberships={sorted(SESSION_DETAILS.team_ids)}).",
        )


async def _run_dm_mode(
    *,
    plan: IngestPlan,
    msg: IngestRequested,
    infra: WorkerInfra,
    run_ledger,
    is_cancelled: CancellationCheck | None = None,
    should_surrender: Callable[[], bool] | None = None,
    journal: RunJournal | NullJournal = NullJournal(),
) -> tuple[int, str | None]:
    """Dispatch an ``IngestPlan`` via raw DataManager ingestion.

    Constructs one ``ArtifactWorkItem`` per table in the plan, then
    drives :func:`unify.common.pipeline.ingest_artifacts` with a DM
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
        # Membership is hydrated onto SESSION_DETAILS by _with_unify_key; reject
        # a team destination the assistant cannot reach before provisioning.
        _validate_team_destination(dm_binding)
        return await _run_dm_mode_inner(
            plan=plan,
            msg=msg,
            infra=infra,
            run_ledger=run_ledger,
            dm_binding=dm_binding,
            default_target=default_target,
            activate_unify_context=activate_unify_context,
            is_cancelled=is_cancelled,
            should_surrender=should_surrender,
            journal=journal,
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
    should_surrender: Callable[[], bool] | None = None,
    journal: RunJournal | NullJournal = NullJournal(),
) -> tuple[int, str | None]:
    """Body of DM dispatch, run inside the per-message UNIFY_KEY scope.

    The ingest itself is :class:`CheckpointedIngest`, which is the same engine
    in-process ingestion uses. That is deliberate and load-bearing: leases,
    checkpoints, the duplicate-delivery fast path and the completion check are
    the guarantees that make a run resumable, and a second copy of them here
    would let the two tiers drift apart in exactly those semantics.

    What remains worker-specific is the surroundings -- resolving the plan's
    tables into work, translating a surrender into a nack, and writing the stage
    manifest.
    """
    from unify.data_manager import DataManager
    from unify.file_manager.types.config import FilePipelineConfig

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

    work = _table_work_from_plan(plan, msg=msg, default_target=default_target)

    engine = CheckpointedIngest(
        artifact_store=infra.artifact_store,
        job_id=msg.job_id,
        lease_ttl_seconds=int(
            getattr(infra.settings, "lease_ttl_seconds", 900) or 900,
        ),
    )

    total_rows = 0
    error: str | None = None
    start = time.perf_counter()
    declared_total = sum(entry.declared_rows for entry in work)
    journal.event(
        stage="ingest",
        state="running",
        total=declared_total or None,
        message=(
            f"Ingesting {len(work)} table(s) from {plan.file_path} "
            "on the worker fleet."
        ),
    )
    try:
        with instrumentation:
            outcome = engine.run(
                work,
                dm=dm,
                destination=dm_binding.destination,
                source_path=plan.file_path,
                instrumentation=instrumentation,
                is_cancelled=is_cancelled,
                on_progress=lambda table_id, done, total: journal.progress(
                    stage="ingest",
                    done=done,
                    total=total or None,
                ),
                # A SIGTERM (HPA scale-down, rollout) or the queue's
                # lease-lifetime cap both mean this attempt must stop owning the
                # message. Surrendering between chunks leaves the checkpoint
                # current and the lease released, so a replacement pod resumes
                # having lost at most one chunk.
                should_surrender=(
                    should_surrender
                    if should_surrender is not None
                    else is_shutdown_requested
                ),
                retry_config=config.retry,
                storage_client=infra.storage_client,
                # Verified by the caller's bounded-resume gate rather than here,
                # so a shortfall becomes a nack-and-resume before it becomes a
                # failure. The gap is still reported on the outcome.
                verify=False,
            )
        total_rows = outcome.rows_committed
        failures = [table for table in outcome.failed if table.error]
        if failures:
            error = failures[0].error or "ingest failed"
    except RetryWorkItem:
        # A surrender: let it reach the entrypoint for an immediate nack and
        # resume-from-checkpoint, rather than finalizing a terminal error that
        # would be acked and never retried.
        raise
    except PipelineCancelled:
        raise
    except DuplicateLiveAttempt:
        raise
    except Exception as exc:
        error = str(exc) or "checkpointed ingest raised"
        logger.exception("[ingest][dm] Failed for %s", plan.file_path)

    journal.event(
        stage="ingest",
        state="succeeded" if error is None else "failed",
        level="info" if error is None else "error",
        done=total_rows,
        total=declared_total or None,
        message=(
            f"Committed {total_rows} row(s) from {plan.file_path} to "
            f"{', '.join(sorted({entry.context for entry in work}))}."
            if error is None
            else f"{plan.file_path}: {error}"
        ),
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
                "table_count": len(work),
                "total_rows": total_rows,
            },
        ),
    )
    return total_rows, error


def _table_work_from_plan(
    plan: IngestPlan,
    *,
    msg: IngestRequested,
    default_target: str,
) -> list[TableWork]:
    """Resolve a plan's tables into work the shared engine can run.

    A table whose row count cannot be resolved is refused rather than ingested
    with an unknown total. The count is what the completion check holds the
    durable checkpoint against, so ingesting without one would produce a run
    that cannot be verified -- and an unverifiable ingest is how a shortfall
    passes as success.
    """
    table_inputs = plan.table_inputs or {}
    work: list[TableWork] = []
    missing_inputs: list[str] = []

    for meta in plan.tables_meta or []:
        table_id = str(meta.table_id or "")
        handle = table_inputs.get(table_id)
        if handle is None:
            missing_inputs.append(table_id or str(meta.label or "unknown"))
            continue

        declared = (
            meta.row_count
            if meta.row_count is not None
            else getattr(handle, "row_count", None)
        )
        if declared is None:
            raise RuntimeError(
                f"Ingest manifest table={table_id} has no declared row_count; "
                "strict row-count validation requires parser counts.",
            )

        post_ingest = None
        if meta.post_ingest:
            from unify.data_manager.types.ingest import PostIngestConfig

            post_ingest = PostIngestConfig.model_validate(meta.post_ingest)

        work.append(
            TableWork(
                table_id=table_id,
                label=str(meta.label or table_id or "table"),
                context=meta.context or default_target,
                handle=handle,
                declared_rows=int(declared),
                columns=list(meta.columns or [])
                or list(getattr(handle, "columns", []) or []),
                chunk_size=meta.chunk_size or msg.batch_size,
                description=meta.description,
                column_descriptions=meta.column_descriptions,
                embed_columns=meta.embed_columns,
                embed_strategy=meta.embed_strategy,
                post_ingest=post_ingest,
                # Scoped to the dispatch as well as the job so two dispatches
                # writing one context cannot collide on a row key.
                ingest_key_prefix=(
                    f"{msg.dispatch_id or 'dispatch'}:{msg.job_id}:{table_id}"
                ),
            ),
        )

    if missing_inputs:
        raise RuntimeError(
            f"Ingest manifest missing table input handles for {missing_inputs}; "
            "refusing partial ingest.",
        )
    if not work and plan.tables_meta:
        raise RuntimeError("Ingest manifest did not produce any table work items.")
    return work


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
