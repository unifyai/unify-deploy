"""GCS-backed DeploymentBundleStore and DeploymentJobStore."""

from __future__ import annotations

import json
import logging

from droid.common.pipeline._utils import utc_now_iso
from droid.common.pipeline.deployment.types import (
    DeploymentBundle,
    DeploymentBundleRef,
    DeploymentIngestionJob,
    DispatchManifest,
)

from .artifact_store import GcsArtifactStore

logger = logging.getLogger(__name__)


class GcsDeploymentBundleStore:
    """GCS-backed bundle store implementing DeploymentBundleStore protocol."""

    def __init__(self, *, artifact_store: GcsArtifactStore):
        self._store = artifact_store

    def write_bundle(self, bundle: DeploymentBundle) -> DeploymentBundleRef:
        key = f"deployments/{bundle.bundle_id}/bundle.json"
        self._store.put_json(key, bundle.model_dump(mode="json"))
        return DeploymentBundleRef(
            bundle_id=bundle.bundle_id,
            manifest_path=key,
        )

    def read_bundle(self, bundle_id: str) -> DeploymentBundle:
        key = f"deployments/{bundle_id}/bundle.json"
        data = self._store.get_json(key)
        return DeploymentBundle.model_validate(data)


class GcsDeploymentJobStore:
    """GCS-backed job store implementing DeploymentJobStore protocol.

    Cancellation is implemented by setting ``status="cancelled"`` on the
    job record.  Workers check this via ``read_job()`` between stages.

    Pause/resume is a weaker variant: ``status="paused"`` tells workers
    to checkpoint the current chunk, ack the in-flight message, park its
    payload in ``dispatches/{dispatch_id}/parked/``, and exit without
    marking the job terminal.  ``resume_job()`` flips the status back to
    ``queued`` so the operator's ``resume`` command can re-publish the
    parked messages and the HPA can scale workers back up on the
    resulting backlog.
    """

    # Jobs in these statuses are terminal and cannot be paused or resumed.
    _TERMINAL_STATUSES = ("success", "error", "cancelled")

    def __init__(self, *, artifact_store: GcsArtifactStore):
        self._store = artifact_store

    def upsert_job(self, job: DeploymentIngestionJob) -> str:
        key = f"jobs/{job.job_id}/job.json"
        self._store.put_json(key, job.model_dump(mode="json"))
        return job.job_id

    def read_job(self, job_id: str) -> DeploymentIngestionJob:
        key = f"jobs/{job_id}/job.json"
        data = self._store.get_json(key)
        return DeploymentIngestionJob.model_validate(data)

    def cancel_job(self, job_id: str, *, reason: str = "") -> DeploymentIngestionJob:
        """Mark a job as cancelled so workers detect it on next checkpoint."""
        job = self.read_job(job_id)
        job.status = "cancelled"
        job.cancelled_at = utc_now_iso()
        job.cancel_reason = reason or "operator-initiated"
        self.upsert_job(job)
        logger.info("Cancelled job %s: %s", job_id, reason or "operator-initiated")
        return job

    def pause_job(self, job_id: str, *, reason: str = "") -> DeploymentIngestionJob:
        """Flip a job to ``status="paused"`` unless already terminal.

        Workers watching ``job.json`` trip a ``pause_event`` and unwind
        their pipeline between chunks, then park the in-flight message
        without flipping the job's status to a terminal value.
        """
        job = self.read_job(job_id)
        if job.status in self._TERMINAL_STATUSES:
            logger.info(
                "pause_job: %s is already %s, no-op",
                job_id,
                job.status,
            )
            return job
        job.status = "paused"
        job.paused_at = utc_now_iso()
        job.pause_reason = reason or "operator-initiated"
        self.upsert_job(job)
        logger.info("Paused job %s: %s", job_id, job.pause_reason)
        return job

    def resume_job(self, job_id: str) -> DeploymentIngestionJob:
        """Flip a paused job back to ``status="queued"``.

        Clears ``paused_at`` and ``pause_reason`` but intentionally
        retains ``parked_manifest_keys`` so re-publish can sort parked
        blobs before removal; the resume flow is responsible for
        emptying the list once the re-publish succeeds.
        """
        job = self.read_job(job_id)
        if job.status != "paused":
            logger.info(
                "resume_job: %s is %s (not paused), no-op",
                job_id,
                job.status,
            )
            return job
        job.status = "queued"
        job.paused_at = None
        job.pause_reason = None
        # Stamp the re-enqueue time so queued-stale detection measures age from
        # this transition (not the original dispatch), avoiding a just-resumed
        # job being misclassified as limbo before its republished message lands.
        job.metadata = {**(job.metadata or {}), "queued_at": utc_now_iso()}
        self.upsert_job(job)
        logger.info("Resumed job %s", job_id)
        return job

    def pause_dispatch(
        self,
        dispatch_id: str,
        *,
        reason: str = "",
    ) -> tuple[int, int]:
        """Mark a dispatch and all its non-terminal jobs as paused.

        Updates ``DispatchManifest.status_override`` to ``"paused"`` and
        then loops over ``manifest.job_ids`` applying ``pause_job`` to
        each.  Returns ``(paused_count, skipped_count)``.
        """
        manifest = self.read_dispatch(dispatch_id)
        manifest.status_override = "paused"
        manifest.paused_at = utc_now_iso()
        manifest.pause_reason = reason or "operator-initiated"
        self.write_dispatch(manifest)

        paused = 0
        skipped = 0
        for jid in manifest.job_ids:
            try:
                job = self.read_job(jid)
            except Exception:
                skipped += 1
                continue
            if job.status in self._TERMINAL_STATUSES or job.status == "paused":
                skipped += 1
                continue
            try:
                self.pause_job(jid, reason=reason)
                paused += 1
            except Exception:
                logger.exception("pause_dispatch: failed on job %s", jid)
                skipped += 1
        logger.info(
            "Paused dispatch %s: %d paused, %d skipped",
            dispatch_id,
            paused,
            skipped,
        )
        return paused, skipped

    def resume_dispatch(self, dispatch_id: str) -> tuple[int, int]:
        """Flip a paused dispatch and all its paused jobs back to ``queued``.

        Returns ``(resumed_count, skipped_count)``.
        """
        manifest = self.read_dispatch(dispatch_id)
        manifest.status_override = "active"
        manifest.paused_at = None
        manifest.pause_reason = None
        self.write_dispatch(manifest)

        resumed = 0
        skipped = 0
        for jid in manifest.job_ids:
            try:
                job = self.read_job(jid)
            except Exception:
                skipped += 1
                continue
            if job.status != "paused":
                skipped += 1
                continue
            try:
                self.resume_job(jid)
                resumed += 1
            except Exception:
                logger.exception("resume_dispatch: failed on job %s", jid)
                skipped += 1
        logger.info(
            "Resumed dispatch %s: %d resumed, %d skipped",
            dispatch_id,
            resumed,
            skipped,
        )
        return resumed, skipped

    # -- dispatch manifest helpers -------------------------------------------

    def write_dispatch(self, manifest: DispatchManifest) -> str:
        """Write a dispatch manifest to ``dispatches/<dispatch_id>/manifest.json``."""
        key = f"dispatches/{manifest.dispatch_id}/manifest.json"
        self._store.put_json(key, manifest.model_dump(mode="json"))
        logger.info(
            "Wrote dispatch manifest %s (%d jobs)",
            manifest.dispatch_id,
            len(manifest.job_ids),
        )
        return key

    def read_dispatch(self, dispatch_id: str) -> DispatchManifest:
        """Read a dispatch manifest by ID."""
        key = f"dispatches/{dispatch_id}/manifest.json"
        data = self._store.get_json(key)
        return DispatchManifest.model_validate(data)

    def list_dispatches(self, *, limit: int = 20) -> list[DispatchManifest]:
        """List recent dispatch manifests, newest first.

        Scans ``dispatches/*/manifest.json`` blobs in the artifact bucket.
        Results are sorted by ``created_at`` descending and capped at *limit*.
        """
        prefix = self._store._full_key("dispatches/")
        bucket = self._store.bucket
        blobs = bucket.list_blobs(prefix=prefix)

        manifests: list[DispatchManifest] = []
        for blob in blobs:
            if not blob.name.endswith("/manifest.json"):
                continue
            try:
                data = json.loads(blob.download_as_text(encoding="utf-8"))
                manifests.append(DispatchManifest.model_validate(data))
            except Exception:
                logger.debug("Skipping unreadable dispatch manifest: %s", blob.name)

        manifests.sort(key=lambda m: m.created_at, reverse=True)
        return manifests[:limit]
