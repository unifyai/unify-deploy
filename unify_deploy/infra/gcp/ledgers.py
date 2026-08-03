"""GCS-backed RunLedger implementation."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from google.cloud import storage

from unify.common.pipeline.retry_policy import ResilientRequestPolicy

if TYPE_CHECKING:
    from pydantic import BaseModel


from .settings import GcsArtifactStoreSettings

logger = logging.getLogger(__name__)


class GcsRunLedger:
    """GCS-backed run ledger writing stage/file/run manifests as JSONL.

    Implements the ``RunLedger`` protocol from unify.  Buffers writes
    in memory and flushes periodically or on explicit ``flush()`` /
    ``close()`` calls to keep GCS round-trips manageable while still
    providing near-real-time visibility.
    """

    def __init__(
        self,
        *,
        client: storage.Client,
        settings: GcsArtifactStoreSettings,
        run_id: str,
        flush_threshold: int = 10,
        retry_policy: ResilientRequestPolicy | None = None,
        blob_basename: str = "run_ledger.jsonl",
    ):
        # ``blob_basename`` lets callers keep multiple append-only JSONL
        # ledgers side-by-side under a single run directory, e.g.::
        #
        #   jobs/{run_id}/run_ledger.jsonl   ← stage/file/run manifests
        #   jobs/{run_id}/heartbeats.jsonl   ← periodic liveness signal
        #
        # Each GcsRunLedger instance rewrites its OWN blob on flush, so
        # two instances sharing the same basename would clobber each
        # other — always pick a unique basename per concurrent writer.
        #
        # The bucket is shared with the artifact store (env-scoped in the
        # bucket name itself, e.g. ``unity-pipeline-artifacts-staging``),
        # so per-env cross-writes are structurally impossible.
        self._client = client
        self._bucket_name = settings.bucket
        prefix = settings.prefix.strip("/")
        job_root = f"{prefix}/jobs/{run_id}" if prefix else f"jobs/{run_id}"
        self._blob_key = f"{job_root}/{blob_basename}"
        self._flush_threshold = max(flush_threshold, 1)
        self._retry_policy = retry_policy or ResilientRequestPolicy()

        self._buffer: list[str] = []
        self._flushed_content: str = ""
        self._lock = threading.Lock()

    def write(self, manifest: "BaseModel") -> None:
        line = manifest.model_dump_json() + "\n"
        with self._lock:
            self._buffer.append(line)
            if len(self._buffer) >= self._flush_threshold:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        self.flush()

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        chunk = "".join(self._buffer)
        self._flushed_content += chunk
        self._buffer.clear()

        blob = self._client.bucket(self._bucket_name).blob(self._blob_key)
        try:
            blob.upload_from_string(
                self._flushed_content,
                content_type="application/x-ndjson",
            )
        except Exception:
            logger.exception("Failed to flush run ledger to GCS: %s", self._blob_key)

    @property
    def gcs_uri(self) -> str:
        return f"gs://{self._bucket_name}/{self._blob_key}"
