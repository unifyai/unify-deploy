"""GCS-backed implementation of the ArtifactStore protocol."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from google.cloud import storage

from unity.common.pipeline.artifact_store import CONTENT_ROWS_TABLE_ID
from unity.common.pipeline.retry_policy import ResilientRequestPolicy
from unity.common.pipeline.row_streaming import iter_table_input_rows
from unity.common.pipeline.types import (
    InlineRowsHandle,
    ObjectStoreArtifactHandle,
    TableInputHandle,
)
from typing import Iterable

from .settings import GcsArtifactStoreSettings

logger = logging.getLogger(__name__)


class GcsArtifactStore:
    """GCS-backed artifact store implementing the unity ArtifactStore protocol."""

    def __init__(
        self,
        *,
        client: storage.Client,
        settings: GcsArtifactStoreSettings,
        retry_policy: ResilientRequestPolicy | None = None,
    ):
        self._client = client
        self._bucket_name = settings.bucket
        self._prefix = settings.prefix.strip("/")
        self._retry_policy = retry_policy or ResilientRequestPolicy()

    @property
    def bucket(self) -> storage.Bucket:
        return self._client.bucket(self._bucket_name)

    def _full_key(self, key: str) -> str:
        key = key.lstrip("/")
        if self._prefix:
            return f"{self._prefix}/{key}"
        return key

    # -- table materialisation -----------------------------------------------

    def materialize_table_input(
        self,
        handle: TableInputHandle,
        *,
        logical_path: str,
        table_id: str,
        artifact_format: str,
    ) -> ObjectStoreArtifactHandle:
        if isinstance(handle, ObjectStoreArtifactHandle):
            return handle
        if artifact_format != "jsonl":
            raise ValueError(
                f"Unsupported artifact format for GcsArtifactStore: {artifact_format!r}",
            )

        blob_key = self._full_key(
            f"artifacts/{_safe_fragment(logical_path)}/{_safe_fragment(table_id)}.jsonl",
        )
        blob = self.bucket.blob(blob_key)

        columns: list[str] = list(getattr(handle, "columns", []) or [])
        row_count = 0

        with blob.open("w", content_type="application/x-ndjson") as writer:
            for row in iter_table_input_rows(handle):
                payload = {str(k): v for k, v in dict(row).items()}
                if not columns:
                    columns = list(payload.keys())
                writer.write(json.dumps(payload, ensure_ascii=False))
                writer.write("\n")
                row_count += 1

        storage_uri = f"gs://{self._bucket_name}/{blob_key}"
        logger.info(
            "Materialized %d rows to %s",
            row_count,
            storage_uri,
        )

        return ObjectStoreArtifactHandle(
            storage_uri=storage_uri,
            logical_path=logical_path,
            artifact_format="jsonl",
            columns=columns,
            row_count=row_count,
        )

    def materialize_content_rows(
        self,
        rows: Iterable[Any],
        *,
        logical_path: str,
        artifact_format: str = "jsonl",
    ) -> ObjectStoreArtifactHandle:
        """Serialise lowered content rows as a JSONL artifact in GCS.

        Rows may be Pydantic models (e.g. ``FileContentRow``) or plain
        dicts.  The table id is fixed to :data:`CONTENT_ROWS_TABLE_ID` so
        manifests reference a single conventional key for derived content.
        """
        serialised: list[dict[str, Any]] = []
        columns: list[str] = []
        for row in rows:
            dump = getattr(row, "model_dump", None)
            if callable(dump):
                payload = dict(dump(mode="json", exclude_none=True))
            elif isinstance(row, dict):
                payload = {str(k): v for k, v in row.items()}
            else:
                payload = {"value": row}
            serialised.append(payload)
            if not columns:
                columns = [str(k) for k in payload.keys()]

        inline = InlineRowsHandle(
            rows=serialised,
            columns=columns,
            row_count=len(serialised),
        )
        return self.materialize_table_input(
            inline,
            logical_path=logical_path,
            table_id=CONTENT_ROWS_TABLE_ID,
            artifact_format=artifact_format,
        )

    # -- manifest CRUD -------------------------------------------------------

    def put_json(self, key: str, data: Any) -> str:
        blob_key = self._full_key(key)
        blob = self.bucket.blob(blob_key)
        content = json.dumps(data, ensure_ascii=False, default=str)
        self._with_retry(
            lambda: blob.upload_from_string(content, content_type="application/json"),
            operation=f"put_json({key})",
        )
        return f"gs://{self._bucket_name}/{blob_key}"

    def get_json(self, key: str) -> Any:
        blob_key = self._full_key(key)
        blob = self.bucket.blob(blob_key)
        content = self._with_retry(
            lambda: blob.download_as_text(encoding="utf-8"),
            operation=f"get_json({key})",
        )
        return json.loads(content)

    def exists(self, key: str) -> bool:
        blob_key = self._full_key(key)
        blob = self.bucket.blob(blob_key)
        return self._with_retry(
            lambda: blob.exists(),
            operation=f"exists({key})",
        )

    def delete(self, key: str) -> None:
        blob_key = self._full_key(key)
        blob = self.bucket.blob(blob_key)
        try:
            self._with_retry(
                lambda: blob.delete(),
                operation=f"delete({key})",
            )
        except Exception:
            pass

    # -- retry wrapper -------------------------------------------------------

    def _with_retry(self, fn, *, operation: str):
        started = time.perf_counter()
        attempt = 0
        while True:
            try:
                return fn()
            except Exception as exc:
                decision = self._retry_policy.check_retry(
                    exc,
                    attempt_index=attempt,
                    started_at=started,
                )
                if not decision.should_retry:
                    raise
                delay = self._retry_policy.compute_delay(attempt_index=attempt)
                logger.warning(
                    "Retrying %s (attempt %d, delay %.1fs): %s",
                    operation,
                    attempt + 1,
                    delay,
                    exc,
                )
                time.sleep(delay)
                attempt += 1


def _safe_fragment(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "artifact"
    return "".join(
        char if char.isalnum() or char in ("-", "_", "/") else "_" for char in text
    )
