"""GCS-backed DeploymentBundleStore and DeploymentJobStore."""

from __future__ import annotations

import json
import logging

from unity.common.pipeline._utils import utc_now_iso
from unity.common.pipeline.deployment.types import (
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
    """

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
