"""GCS-backed DeploymentBundleStore and DeploymentJobStore."""

from __future__ import annotations

import logging

from unity.common.pipeline._utils import utc_now_iso
from unity.common.pipeline.deployment.types import (
    DeploymentBundle,
    DeploymentBundleRef,
    DeploymentIngestionJob,
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
