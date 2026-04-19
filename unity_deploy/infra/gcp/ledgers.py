"""GCS-backed RunLedger and CostLedger implementations."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from google.cloud import storage

from unity.common.pipeline.retry_policy import ResilientRequestPolicy

if TYPE_CHECKING:
    from pydantic import BaseModel

    from unity.common.pipeline.cost_ledger import PipelineCostLedger

from .settings import GcsLedgerSettings

logger = logging.getLogger(__name__)


class GcsRunLedger:
    """GCS-backed run ledger writing stage/file/run manifests as JSONL.

    Implements the ``RunLedger`` protocol from unity.  Buffers writes
    in memory and flushes periodically or on explicit ``flush()`` /
    ``close()`` calls to keep GCS round-trips manageable while still
    providing near-real-time visibility.
    """

    def __init__(
        self,
        *,
        client: storage.Client,
        settings: GcsLedgerSettings,
        run_id: str,
        environment: str = "staging",
        flush_threshold: int = 10,
        retry_policy: ResilientRequestPolicy | None = None,
    ):
        self._client = client
        self._bucket_name = settings.bucket
        prefix = settings.prefix.strip("/")
        env_prefix = f"{prefix}/{environment}" if prefix else environment
        self._blob_key = f"{env_prefix}/{run_id}/run_ledger.jsonl"
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


class GcsCostLedger:
    """GCS-backed cost ledger writing the final cost ledger as JSON.

    Implements the ``CostLedger`` protocol from unity.
    """

    def __init__(
        self,
        *,
        client: storage.Client,
        settings: GcsLedgerSettings,
        run_id: str,
        environment: str = "staging",
        retry_policy: ResilientRequestPolicy | None = None,
    ):
        self._client = client
        self._bucket_name = settings.bucket
        prefix = settings.prefix.strip("/")
        env_prefix = f"{prefix}/{environment}" if prefix else environment
        self._blob_key = f"{env_prefix}/{run_id}/cost_ledger.json"
        self._retry_policy = retry_policy or ResilientRequestPolicy()

        self._latest: "PipelineCostLedger | None" = None
        self._lock = threading.Lock()

    def write(self, ledger: "PipelineCostLedger") -> None:
        with self._lock:
            self._latest = ledger

    def flush(self) -> None:
        with self._lock:
            ledger = self._latest
        if ledger is None:
            return
        blob = self._client.bucket(self._bucket_name).blob(self._blob_key)
        content = ledger.model_dump_json(indent=2)
        try:
            blob.upload_from_string(content, content_type="application/json")
        except Exception:
            logger.exception("Failed to flush cost ledger to GCS: %s", self._blob_key)

    def close(self) -> None:
        self.flush()

    @property
    def gcs_uri(self) -> str:
        return f"gs://{self._bucket_name}/{self._blob_key}"
