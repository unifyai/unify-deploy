"""Pipeline operations the assistant may ask for, independent of any transport.

The operator CLI drives these same primitives through argparse; the hosted
control plane at ``/infra/pipeline/*`` drives them over HTTP on behalf of an
assistant pod. Keeping the operations here rather than in either caller is what
lets the two agree: a retry asked for by the actor and a retry asked for by an
engineer take the same path, so a guarantee proven in one holds in the other.

Two things distinguish this module from the CLI's own command bodies.

**Recovery is serialised on a lease, not on a warning.** The CLI documents that
``retry`` and ``recover-stale`` must not run concurrently, and guards it with a
freshness window plus a ``--force`` escape. Two publishes for one job put two
live messages against one attempt-lease, and the loser's writes freeze the
durable checkpoint -- the run then under-ingests while reporting nothing. A
window narrows that race; it does not remove it, and ``--force`` re-opens it on
purpose. Here every recovery transition takes an exclusive lease on the job
first, so a second attempt is told who holds it rather than racing. There is no
force parameter on this surface: an assistant cannot be trusted to know that a
prior message is really gone, and neither can an operator in a hurry.

**Nothing here trusts a caller for identity.** Bindings are built from the
verified caller, never from a payload. The routes authenticate the assistant and
pass its own ids in; this module has no way to accept another tenant's.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from unify.common.pipeline._utils import utc_now_iso
from unify.common.pipeline.artifact_store import LeaseNotAcquired

logger = logging.getLogger(__name__)

# How long a recovery transition may hold a job before a peer may take over.
# Short: the lease covers publishing a message and updating the job row, not
# the ingestion itself. Long enough that a slow GCS round trip does not lose it.
RECOVERY_LEASE_TTL_SECONDS = 120
RECOVERY_LEASE_STEAL_AFTER_SECONDS = 30

# Terminal job statuses, past which no recovery verb applies.
TERMINAL_JOB_STATUSES = frozenset({"success", "error", "cancelled"})


class PipelineOpError(RuntimeError):
    """An operation could not be performed, with a caller-facing reason."""

    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


class RecoveryBusy(PipelineOpError):
    """Another recovery attempt owns this job.

    Distinct from a failure: the correct response is to let the holder finish
    and ask again, which is exactly what the message says.
    """

    def __init__(self, job_id: str, holder: str):
        super().__init__(
            f"Job {job_id} is being recovered by {holder}; nothing was published. "
            "Ask again once it finishes.",
            status_code=409,
        )


def _recovery_lease_key(job_id: str) -> str:
    return f"jobs/{job_id}/leases/recovery.json"


@dataclass
class _RecoveryLease:
    """An exclusive claim on one job's recovery transition."""

    store: Any
    key: str
    owner_id: str
    attempt_id: str
    generation: int | None = None

    def release(self) -> None:
        try:
            self.store.release_lease(
                self.key,
                owner_id=self.owner_id,
                attempt_id=self.attempt_id,
                generation=self.generation,
            )
        except Exception:
            # A lost lease is already someone else's; leaving it alone is
            # correct, and failing the caller over cleanup would be worse than
            # waiting out a two-minute TTL.
            logger.info("Recovery lease %s was already taken over", self.key)


def hold_recovery(artifact_store: Any, job_id: str) -> _RecoveryLease:
    """Take the exclusive right to publish a recovery message for *job_id*.

    Raises :class:`RecoveryBusy` when another attempt holds it. This is the one
    mechanism that makes retry and stale-recovery mutually exclusive; callers
    must wrap every publish-and-update sequence in it.
    """
    key = _recovery_lease_key(job_id)
    attempt_id = uuid.uuid4().hex
    owner_id = f"recovery-{attempt_id[:12]}"
    try:
        lease = artifact_store.acquire_lease(
            key,
            owner_id=owner_id,
            attempt_id=attempt_id,
            stage="recovery",
            ttl_seconds=RECOVERY_LEASE_TTL_SECONDS,
            steal_expired_after_seconds=RECOVERY_LEASE_STEAL_AFTER_SECONDS,
        )
    except LeaseNotAcquired as exc:
        holder = getattr(exc.lease, "owner_id", "another attempt")
        raise RecoveryBusy(job_id, holder) from exc
    return _RecoveryLease(
        store=artifact_store,
        key=key,
        owner_id=owner_id,
        attempt_id=attempt_id,
        generation=getattr(lease, "generation", None),
    )


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


@dataclass
class UploadTarget:
    """Where one source file's bytes should be put, and how to name it later."""

    logical_path: str
    upload_url: str
    object_uri: str


@dataclass
class SubmitPreparation:
    """Everything the caller needs to stage a run's bytes before publishing."""

    dispatch_id: str
    request_upload: UploadTarget
    sources: list[UploadTarget] = field(default_factory=list)


def prepare_submit(
    *,
    artifact_store: Any,
    run_key: str,
    request_key: str,
    paths: list[str],
    upload_ttl_seconds: int = 900,
) -> SubmitPreparation:
    """Mint write targets for a run's staged request and its source files.

    The pod uploads to these and then asks for the publish. Brokering the
    targets rather than the bytes is what keeps the assistant credential-poor
    while still letting a multi-gigabyte file reach the fleet: the control plane
    never sees the payload, and no request body carries it, so neither the
    request-size ceiling nor the plane's own bandwidth bounds an ingestion.

    ``dispatch_id`` is the run's own key, not a fresh id. One identity across
    the run row, the artifacts, the leases and the checkpoints is what makes a
    dispatched run resumable by anything that can read the layout.
    """
    request_target = _upload_target(
        artifact_store,
        logical_path="request.json",
        key=request_key,
        ttl_seconds=upload_ttl_seconds,
    )
    sources = [
        _upload_target(
            artifact_store,
            logical_path=path,
            key=f"jobs/{run_key}/sources/{index:04d}-{_safe_name(path)}",
            ttl_seconds=upload_ttl_seconds,
        )
        for index, path in enumerate(paths)
    ]
    return SubmitPreparation(
        dispatch_id=run_key,
        request_upload=request_target,
        sources=sources,
    )


def _safe_name(path: str) -> str:
    base = str(path).replace("\\", "/").rsplit("/", 1)[-1] or "source"
    return "".join(
        char if char.isalnum() or char in ("-", "_", ".") else "_" for char in base
    )[-96:]


def _upload_target(
    artifact_store: Any,
    *,
    logical_path: str,
    key: str,
    ttl_seconds: int,
) -> UploadTarget:
    """One write target, signed where the backing store can sign.

    An object store issues a signed PUT so the bytes go straight to it. A
    filesystem-backed store cannot sign anything, so it reports the key and the
    caller writes through the control plane's own upload route -- which is the
    self-host shape, where the plane and the workers share a volume.
    """
    signer = getattr(artifact_store, "signed_upload_url", None)
    if callable(signer):
        url, uri = signer(key, ttl_seconds=ttl_seconds)
        return UploadTarget(logical_path=logical_path, upload_url=url, object_uri=uri)
    return UploadTarget(
        logical_path=logical_path,
        upload_url="",
        object_uri=key,
    )


def _execution_target(environment: str) -> str:
    """Map a deployment environment onto a ``DeploymentExecutionTarget``.

    The two vocabularies overlap without being the same. Environment detection
    answers "which deployment is this" and can say ``development``; the job
    model's target answers "where does this execute" and accepts only ``local``,
    ``local_with_gcp``, ``staging`` and ``production``. Feeding one straight
    into the other is what sent ``hosted`` -- and would have sent
    ``development`` from any self-host deployment -- into a field that rejects
    both, surfacing as a 500 from the control plane rather than a named bad
    value.
    """
    return {
        "development": "local",
        "local": "local",
        "local_with_gcp": "local_with_gcp",
        "staging": "staging",
        "production": "production",
    }.get((environment or "").strip().lower(), "production")


def publish_submit(
    *,
    infra: Any,
    run_key: str,
    request_key: str,
    sources: list[UploadTarget],
    ingestion_mode: str,
    user_id: str,
    assistant_id: str,
    destination: str | None,
    target_context: str,
    observability: dict[str, str] | None,
) -> list[str]:
    """Publish one ``ParseRequested`` per staged source and record the dispatch.

    One message per file rather than one naming many: the fleet parallelises by
    message, so a single message would parse every file on one pod and lose the
    isolation the dispatch was chosen for.

    The job rows are written *before* the messages are published, so a crash
    between the two leaves work that can be found and re-driven rather than
    messages nobody recorded.
    """
    from unify.common.pipeline import DispatchTarget, publish_parse_request
    from unify.common.pipeline.deployment.types import (
        DeploymentBundleRef,
        DeploymentIngestionJob,
        DispatchManifest,
    )
    from unify.common.pipeline.types import DmBinding, FmBinding

    settings = infra.settings
    target = DispatchTarget(
        project_id=settings.pubsub.project_id,
        bucket_name=settings.artifact_store.bucket,
        env_suffix=settings.env_suffix(),
    )

    fm_binding = None
    dm_binding = None
    if ingestion_mode == "fm":
        fm_binding = FmBinding(
            user_id=user_id,
            assistant_id=assistant_id,
            fm_alias="Local",
            logical_path="",
            destination=destination,
        )
    else:
        dm_binding = DmBinding(
            user_id=user_id,
            assistant_id=assistant_id,
            target_context=target_context,
            destination=destination,
        )

    job_ids: list[str] = []
    for index, source in enumerate(sources):
        # Job identity is derived from the run and the file's position, so a
        # replayed publish addresses the same job rather than minting a second
        # one for work that may already be part-done.
        job_id = f"{run_key}-{index:04d}"
        binding = (
            fm_binding.model_copy(update={"logical_path": source.logical_path})
            if fm_binding is not None
            else None
        )
        infra.job_store.upsert_job(
            DeploymentIngestionJob(
                job_id=job_id,
                dispatch_id=run_key,
                bundle_ref=DeploymentBundleRef(bundle_id=job_id, manifest_path=""),
                run_mode="file_manager" if ingestion_mode == "fm" else "data_manager",
                # The deployment's own environment, as the CLI publish path
                # already does. "hosted" is not a member of
                # DeploymentExecutionTarget, so every brokered publish was
                # refused by model validation before a single file was parsed --
                # and the refusal surfaced as a 500 from the control plane,
                # which reads as an outage rather than a bad field.
                execution_target=_execution_target(settings.environment),
                status="queued",
                metadata={
                    "source_file": source.logical_path,
                    "target_context": target_context,
                    "queued_at": utc_now_iso(),
                    "request_key": request_key,
                },
            ),
        )
        result = publish_parse_request(
            target=target,
            logical_path=source.logical_path,
            ingestion_mode=ingestion_mode,  # type: ignore[arg-type]
            fm_binding=binding,
            dm_binding=dm_binding,
            dispatch_id=run_key,
            job_id=job_id,
            # Already staged by the caller's upload, so nothing is re-sent.
            source_gs_uri=source.object_uri,
            request_key=request_key,
            observability=observability,
        )
        job_ids.append(result.job_id)
        logger.info(
            "Published parse request run=%s job=%s file=%s message=%s",
            run_key,
            result.job_id,
            source.logical_path,
            result.message_id,
        )

    infra.job_store.write_dispatch(
        DispatchManifest(
            dispatch_id=run_key,
            source="ingestion_manager",
            mode=ingestion_mode,
            config_path="",
            job_ids=job_ids,
            total_files=len(sources),
        ),
    )
    return job_ids


# ---------------------------------------------------------------------------
# Observing
# ---------------------------------------------------------------------------


def job_checkpoints(artifact_store: Any, job_id: str) -> list[Any]:
    """Every checkpoint one job wrote, read through the port.

    Read through the port rather than a backend helper because this runs against
    whichever store the deployment bound -- an object store hosted, a shared
    volume in self-host. A GCS-specific enumeration here would make the whole
    control plane hosted-only, and self-host would lose recovery entirely.
    """
    from unify.common.pipeline.types import IngestCheckpoint

    checkpoints: list[Any] = []
    for key in artifact_store.list_keys(f"jobs/{job_id}/checkpoints/"):
        try:
            checkpoints.append(
                IngestCheckpoint.model_validate(
                    artifact_store.get_json(key),
                ),
            )
        except Exception:
            logger.debug("Unreadable checkpoint %s", key, exc_info=True)
    return checkpoints


def job_dlq_records(artifact_store: Any, job_id: str) -> list[dict[str, Any]]:
    """Every parked record one job left, oldest first, read through the port."""
    records: list[dict[str, Any]] = []
    for key in artifact_store.list_keys(f"jobs/{job_id}/dlq/"):
        try:
            payload = artifact_store.get_json(key)
        except Exception:
            logger.debug("Unreadable DLQ record %s", key, exc_info=True)
            continue
        if isinstance(payload, dict):
            records.append(payload)
    records.sort(key=lambda record: str(record.get("recorded_at") or ""))
    return records


def dispatch_status(*, infra: Any, dispatch_id: str) -> dict[str, Any]:
    """Aggregate a dispatch into the one shape the caller's run row needs.

    Deliberately not the CLI's per-job table: the assistant asks "is this
    finished, did it all land, and is anything stuck", and answering that means
    folding jobs into one state. The rule is conservative -- a dispatch is only
    ``succeeded`` when every job is, and any live job keeps the whole run live,
    because reporting completion while a worker is still writing is the one
    answer that makes a caller act wrongly.
    """
    job_store = infra.job_store
    artifact_store = infra.artifact_store

    try:
        manifest = job_store.read_dispatch(dispatch_id)
        job_ids = list(manifest.job_ids)
    except Exception as exc:
        raise PipelineOpError(
            f"No dispatch {dispatch_id!r} is recorded.",
            status_code=404,
        ) from exc

    statuses: list[str] = []
    rows = 0
    parked = 0
    files_done = 0
    contexts: dict[str, None] = {}
    errors: list[str] = []

    for job_id in job_ids:
        try:
            job = job_store.read_job(job_id)
        except Exception:
            statuses.append("unknown")
            continue
        statuses.append(str(job.status or "unknown"))
        metadata = job.metadata or {}
        if job.status == "success":
            files_done += 1
        if job.error:
            errors.append(f"{job_id}: {job.error}")
        # FM jobs write documents plus extracted tables wherever the file
        # pipeline placed them, so the finished job records the concrete paths;
        # target_context covers DM jobs and anything not yet finished.
        written = metadata.get("contexts")
        if isinstance(written, list) and written:
            for context in written:
                if context:
                    contexts.setdefault(str(context), None)
        else:
            context = str(metadata.get("target_context") or "")
            if context:
                contexts.setdefault(context, None)
        # Rows come from the checkpoints rather than the job metadata: the
        # checkpoint is what a resume trusts, so reporting anything else would
        # let the two disagree about where the run got to.
        for checkpoint in job_checkpoints(artifact_store, job_id):
            rows += int(getattr(checkpoint, "rows_committed", 0) or 0)
        parked += len(job_dlq_records(artifact_store, job_id))

    return {
        "dispatch_id": dispatch_id,
        "state": _fold_state(statuses),
        "jobs": len(job_ids),
        "rows_written": rows,
        "files_processed": files_done,
        "parked": parked,
        "contexts": list(contexts),
        "error": "; ".join(errors[:5]) or None,
    }


def _fold_state(statuses: list[str]) -> str:
    """One run state from many job states, biased against false completion."""
    if not statuses:
        return "queued"
    if any(status in ("queued", "running", "unknown") for status in statuses):
        return "running" if "running" in statuses else "queued"
    if all(status == "success" for status in statuses):
        return "succeeded"
    if any(status == "cancelled" for status in statuses) and not any(
        status == "error" for status in statuses
    ):
        return "cancelled"
    if all(status in ("success", "paused") for status in statuses):
        return "paused"
    return "failed"


# ---------------------------------------------------------------------------
# Recovering
# ---------------------------------------------------------------------------


async def retry_dispatch(
    *,
    infra: Any,
    dispatch_id: str,
    scope: str,
) -> dict[str, Any]:
    """Re-attempt part of a dispatch, one job at a time under its lease.

    ``scope`` follows the manager's vocabulary: ``dlq`` re-publishes only what
    was parked after exhausting retries, ``stale`` only what a worker claimed
    and then stopped reporting, ``all`` everything non-terminal with its
    checkpoints discarded first -- the one scope that rewrites rows that were
    already correct, which is why it is never the default.
    """
    job_store = infra.job_store
    artifact_store = infra.artifact_store

    try:
        manifest = job_store.read_dispatch(dispatch_id)
    except Exception as exc:
        raise PipelineOpError(
            f"No dispatch {dispatch_id!r} is recorded.",
            status_code=404,
        ) from exc

    requeued: list[str] = []
    skipped: list[dict[str, str]] = []

    for job_id in manifest.job_ids:
        try:
            job = job_store.read_job(job_id)
        except Exception:
            skipped.append({"job_id": job_id, "reason": "no job record"})
            continue

        status = str(job.status or "")
        if scope != "all" and status == "success":
            skipped.append({"job_id": job_id, "reason": "already succeeded"})
            continue
        if status in ("queued", "running") and scope != "all":
            # A live attempt owns it. Publishing beside it is the duplicate
            # message this whole module exists to prevent.
            skipped.append({"job_id": job_id, "reason": f"still {status}"})
            continue

        payload, topic = _recovery_payload(artifact_store, job_id, scope=scope)
        if payload is None:
            skipped.append({"job_id": job_id, "reason": "nothing to re-publish"})
            continue

        lease = hold_recovery(artifact_store, job_id)
        try:
            if scope == "all":
                artifact_store.delete_checkpoints(job_id)
            message_id = await infra.work_queue.publish(topic=topic, payload=payload)
            job.status = "queued"
            job.error = None
            job.finished_at = None
            job.metadata = {
                **(job.metadata or {}),
                "retry_scope": scope,
                "retry_source": "infra/pipeline/retry",
                "last_retry_message_id": message_id,
                "last_publish_at": utc_now_iso(),
                "queued_at": utc_now_iso(),
            }
            job_store.upsert_job(job)
            requeued.append(job_id)
        finally:
            lease.release()

    return {
        "dispatch_id": dispatch_id,
        "scope": scope,
        "requeued": len(requeued),
        "job_ids": requeued,
        "skipped": skipped,
    }


def _recovery_payload(
    artifact_store: Any,
    job_id: str,
    *,
    scope: str,
) -> tuple[dict[str, Any] | None, str]:
    """The message to re-publish for one job, and which topic it belongs on.

    A parked DLQ record is preferred when there is one: it is the exact message
    that failed, so re-publishing it resumes precisely the work that stopped. A
    stale or full re-attempt falls back to the parse stage's outbox, which is
    the ingest message the parse worker already committed to -- re-parsing a
    file whose parse succeeded would redo the expensive half for nothing.
    """
    if scope in ("dlq", "all"):
        for record in reversed(job_dlq_records(artifact_store, job_id)):
            payload = record.get("payload")
            topic = str(record.get("retry_topic") or "")
            if isinstance(payload, dict) and topic in ("parse", "ingest"):
                return dict(payload), topic
        if scope == "dlq":
            return None, ""

    try:
        outbox = artifact_store.get_json(f"jobs/{job_id}/outbox/parse.json")
    except Exception:
        return None, ""
    payload = outbox.get("payload") if isinstance(outbox, dict) else None
    if isinstance(payload, dict):
        return payload, "ingest"
    return None, ""


def cancel_dispatch(*, infra: Any, dispatch_id: str) -> dict[str, Any]:
    """Abandon a dispatch's remaining work, keeping what already committed.

    Workers observe the flag at their next chunk boundary, so this returns as
    soon as the intent is durable rather than waiting for them to notice.
    """
    job_store = infra.job_store
    try:
        manifest = job_store.read_dispatch(dispatch_id)
    except Exception as exc:
        raise PipelineOpError(
            f"No dispatch {dispatch_id!r} is recorded.",
            status_code=404,
        ) from exc

    cancelled = 0
    for job_id in manifest.job_ids:
        try:
            job = job_store.read_job(job_id)
            if str(job.status or "") in TERMINAL_JOB_STATUSES:
                continue
            job_store.cancel_job(job_id, reason="cancelled by the assistant")
            cancelled += 1
        except Exception:
            logger.debug("Could not cancel job %s", job_id, exc_info=True)
    return {"dispatch_id": dispatch_id, "cancelled": cancelled}


def pause_dispatch(*, infra: Any, dispatch_id: str) -> dict[str, Any]:
    """Stop a dispatch but keep its outstanding work.

    The job rows flip first so a worker picking a message up mid-pause parks it
    instead of processing it. Draining the backlog into the parking lot is the
    workers' own job, in publish order, which is what lets resume replay them
    without reordering.
    """
    job_store = infra.job_store
    try:
        paused, skipped = job_store.pause_dispatch(
            dispatch_id,
            reason="paused by the assistant",
        )
    except Exception as exc:
        raise PipelineOpError(
            f"No dispatch {dispatch_id!r} is recorded.",
            status_code=404,
        ) from exc
    return {"dispatch_id": dispatch_id, "paused": paused, "skipped": skipped}


def resume_dispatch(*, infra: Any, dispatch_id: str) -> dict[str, Any]:
    """Replay a paused dispatch's parked work in its original order."""
    job_store = infra.job_store
    try:
        resumed, skipped = job_store.resume_dispatch(dispatch_id)
    except Exception as exc:
        raise PipelineOpError(
            f"No dispatch {dispatch_id!r} is recorded.",
            status_code=404,
        ) from exc
    return {"dispatch_id": dispatch_id, "resumed": resumed, "skipped": skipped}
