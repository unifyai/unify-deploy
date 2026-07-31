"""GCS-backed implementation of the ArtifactStore protocol.

Leases, checkpoints and the errors they raise are defined by the port in
``unify.common.pipeline.artifact_store`` and imported here, not redeclared. The
worker handlers are shared across bindings, so a duplicate definition would let
this backend and the local one drift apart in exactly the semantics --
monotonicity, generation fencing, takeover of an expired holder -- that make a
run resumable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

from unify.common.pipeline.artifact_store import (
    CONTENT_ROWS_TABLE_ID,
    ArtifactNotFound,
    LeaseNotAcquired,
    LeaseRecord,
    StaleLeaseError,
)
from unify.common.pipeline.retry_policy import ResilientRequestPolicy
from unify.common.pipeline.row_streaming import iter_table_input_rows
from unify.common.pipeline.types import (
    IngestCheckpoint,
    InlineRowsHandle,
    ObjectStoreArtifactHandle,
    TableInputHandle,
)
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

    # -- persistent worker leases -------------------------------------------

    def acquire_lease(
        self,
        key: str,
        *,
        owner_id: str,
        attempt_id: str,
        stage: str,
        ttl_seconds: int = 900,
        steal_expired_after_seconds: int = 30,
    ) -> LeaseRecord:
        """Create or take over a GCS-backed lease using generation fencing."""

        now = _utc_now()
        lease_key = self._full_key(key)
        blob = self.bucket.blob(lease_key)
        record = self._lease_record(
            key=key,
            owner_id=owner_id,
            attempt_id=attempt_id,
            stage=stage,
            now=now,
            ttl_seconds=ttl_seconds,
        )
        payload = json.dumps(record, ensure_ascii=False, default=str)

        try:
            self._with_retry(
                lambda: blob.upload_from_string(
                    payload,
                    content_type="application/json",
                    if_generation_match=0,
                ),
                operation=f"acquire_lease({key}:create)",
            )
            blob.reload()
            return LeaseRecord(**record, generation=blob.generation)
        except PreconditionFailed:
            pass

        existing, generation = self._read_lease_payload(blob, key)
        existing_record = LeaseRecord(**existing, generation=generation)
        if (
            existing_record.owner_id == owner_id
            and existing_record.attempt_id == attempt_id
        ):
            return self.refresh_lease(
                key,
                owner_id=owner_id,
                attempt_id=attempt_id,
                generation=generation,
                ttl_seconds=ttl_seconds,
            )

        if not _lease_is_expired(
            existing_record,
            grace_seconds=steal_expired_after_seconds,
        ):
            raise LeaseNotAcquired(
                f"Lease {key!r} is owned by {existing_record.owner_id!r} "
                f"until {existing_record.expires_at}",
                lease=existing_record,
            )

        takeover = self._lease_record(
            key=key,
            owner_id=owner_id,
            attempt_id=attempt_id,
            stage=stage,
            now=now,
            ttl_seconds=ttl_seconds,
            takeover_count=existing_record.takeover_count + 1,
            previous_owner_id=existing_record.owner_id,
        )
        self._with_retry(
            lambda: blob.upload_from_string(
                json.dumps(takeover, ensure_ascii=False, default=str),
                content_type="application/json",
                if_generation_match=generation,
            ),
            operation=f"acquire_lease({key}:takeover)",
        )
        blob.reload()
        return LeaseRecord(**takeover, generation=blob.generation)

    def refresh_lease(
        self,
        key: str,
        *,
        owner_id: str,
        attempt_id: str,
        generation: int | None,
        ttl_seconds: int = 900,
    ) -> LeaseRecord:
        """Renew an existing lease and return the new fenced generation."""

        lease_key = self._full_key(key)
        blob = self.bucket.blob(lease_key)
        current, current_generation = self._read_lease_payload(blob, key)
        current_record = LeaseRecord(**current, generation=current_generation)
        if (
            current_record.owner_id != owner_id
            or current_record.attempt_id != attempt_id
        ):
            raise StaleLeaseError(
                f"Lease {key!r} is owned by {current_record.owner_id!r}/"
                f"{current_record.attempt_id!r}, not {owner_id!r}/{attempt_id!r}",
            )
        if generation is not None and current_generation != generation:
            raise StaleLeaseError(
                f"Lease {key!r} generation changed from {generation} "
                f"to {current_generation}",
            )

        now = _utc_now()
        renewed = dict(current)
        renewed["heartbeat_at"] = now.isoformat()
        renewed["expires_at"] = (now + timedelta(seconds=ttl_seconds)).isoformat()
        self._with_retry(
            lambda: blob.upload_from_string(
                json.dumps(renewed, ensure_ascii=False, default=str),
                content_type="application/json",
                if_generation_match=current_generation,
            ),
            operation=f"refresh_lease({key})",
        )
        blob.reload()
        return LeaseRecord(**renewed, generation=blob.generation)

    def verify_lease(
        self,
        key: str,
        *,
        owner_id: str,
        attempt_id: str,
    ) -> LeaseRecord:
        """Read a lease and fail if the caller is no longer the owner."""

        blob = self.bucket.blob(self._full_key(key))
        payload, generation = self._read_lease_payload(blob, key)
        record = LeaseRecord(**payload, generation=generation)
        if record.owner_id != owner_id or record.attempt_id != attempt_id:
            raise StaleLeaseError(
                f"Lease {key!r} owner changed to {record.owner_id!r}/"
                f"{record.attempt_id!r}",
            )
        return record

    def release_lease(
        self,
        key: str,
        *,
        owner_id: str,
        attempt_id: str,
        generation: int | None,
    ) -> None:
        """Best-effort delete for a lease owned by this attempt."""

        record = self.verify_lease(key, owner_id=owner_id, attempt_id=attempt_id)
        blob = self.bucket.blob(self._full_key(key))
        expected = generation if generation is not None else record.generation
        try:
            self._with_retry(
                lambda: blob.delete(if_generation_match=expected),
                operation=f"release_lease({key})",
            )
        except NotFound:
            return

    def _lease_record(
        self,
        *,
        key: str,
        owner_id: str,
        attempt_id: str,
        stage: str,
        now: datetime,
        ttl_seconds: int,
        takeover_count: int = 0,
        previous_owner_id: str = "",
    ) -> dict[str, Any]:
        return {
            "key": key,
            "owner_id": owner_id,
            "attempt_id": attempt_id,
            "stage": stage,
            "acquired_at": now.isoformat(),
            "heartbeat_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "takeover_count": takeover_count,
            "previous_owner_id": previous_owner_id,
        }

    def _read_lease_payload(
        self,
        blob: storage.Blob,
        key: str,
    ) -> tuple[dict[str, Any], int | None]:
        try:
            blob.reload()
            generation = blob.generation
            content = self._with_retry(
                lambda: blob.download_as_text(
                    encoding="utf-8",
                    if_generation_match=generation,
                ),
                operation=f"read_lease({key})",
            )
            data = json.loads(content)
        except NotFound:
            raise LeaseNotAcquired(f"Lease {key!r} disappeared during acquisition")
        if not isinstance(data, dict):
            raise ValueError(f"Lease {key!r} is not a JSON object")
        return data, generation

    # -- table materialisation -----------------------------------------------

    def materialize_table_input(
        self,
        handle: TableInputHandle,
        *,
        logical_path: str,
        table_id: str,
        artifact_format: str,
        job_id: str = "",
    ) -> ObjectStoreArtifactHandle:
        if isinstance(handle, ObjectStoreArtifactHandle):
            return handle
        if artifact_format != "jsonl":
            raise ValueError(
                f"Unsupported artifact format for GcsArtifactStore: {artifact_format!r}",
            )
        if not job_id:
            raise ValueError(
                "GcsArtifactStore.materialize_table_input requires a non-empty "
                "job_id: every materialised artifact lives under "
                "jobs/<job_id>/artifacts/... so a job can be inspected or "
                "purged as a single self-contained directory.",
            )

        blob_key = self._full_key(
            f"jobs/{_safe_fragment(job_id)}/artifacts/{_safe_fragment(table_id)}.jsonl",
        )
        tmp_blob_key = self._full_key(
            "tmp/artifacts/"
            f"{_safe_fragment(job_id)}-{_safe_fragment(table_id)}-{uuid.uuid4().hex}.jsonl",
        )
        tmp_blob = self.bucket.blob(tmp_blob_key)

        columns: list[str] = list(getattr(handle, "columns", []) or [])
        row_count = 0
        checksum = hashlib.sha256()

        t0 = time.perf_counter()
        with tmp_blob.open("w", content_type="application/x-ndjson") as writer:
            for row in iter_table_input_rows(handle):
                payload = {str(k): v for k, v in dict(row).items()}
                if not columns:
                    columns = list(payload.keys())
                line = json.dumps(payload, ensure_ascii=False)
                checksum.update(line.encode("utf-8"))
                checksum.update(b"\n")
                writer.write(line)
                writer.write("\n")
                row_count += 1
        tmp_blob.metadata = {
            "unity-row-count": str(row_count),
            "unity-sha256": checksum.hexdigest(),
        }
        self._with_retry(
            lambda: tmp_blob.patch(),
            operation=f"patch_tmp_artifact_metadata({tmp_blob_key})",
        )

        final_blob = self._with_retry(
            lambda: self.bucket.copy_blob(
                tmp_blob,
                self.bucket,
                new_name=blob_key,
            ),
            operation=f"promote_artifact({blob_key})",
        )
        final_blob.reload()
        final_metadata = final_blob.metadata or {}
        if (
            final_metadata.get("unity-row-count") != str(row_count)
            or final_metadata.get("unity-sha256") != checksum.hexdigest()
        ):
            raise RuntimeError(
                f"Promoted artifact metadata mismatch for gs://{self._bucket_name}/"
                f"{blob_key}",
            )
        try:
            tmp_blob.delete()
        except Exception:
            logger.debug("Failed to delete temporary artifact %s", tmp_blob_key)
        elapsed = time.perf_counter() - t0

        storage_uri = f"gs://{self._bucket_name}/{blob_key}"
        logger.info(
            "Materialized %d rows to %s in %.1fs",
            row_count,
            storage_uri,
            elapsed,
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
        job_id: str = "",
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
            job_id=job_id,
        )

    # -- manifest CRUD -------------------------------------------------------

    def put_json(
        self,
        key: str,
        data: Any,
        *,
        if_generation_match: int | None = None,
    ) -> str:
        blob_key = self._full_key(key)
        blob = self.bucket.blob(blob_key)
        content = json.dumps(data, ensure_ascii=False, default=str)
        kwargs: dict[str, Any] = {}
        if if_generation_match is not None:
            kwargs["if_generation_match"] = if_generation_match
        self._with_retry(
            lambda: blob.upload_from_string(
                content,
                content_type="application/json",
                **kwargs,
            ),
            operation=f"put_json({key})",
        )
        return f"gs://{self._bucket_name}/{blob_key}"

    def get_json(self, key: str) -> Any:
        blob_key = self._full_key(key)
        blob = self.bucket.blob(blob_key)
        try:
            content = self._with_retry(
                lambda: blob.download_as_text(encoding="utf-8"),
                operation=f"get_json({key})",
            )
        except NotFound as exc:
            # Translated to the port's type so a caller distinguishing "absent"
            # from "unreachable" -- reading a checkpoint that may not exist yet
            # is the common one -- works the same against either binding.
            raise ArtifactNotFound(f"Artifact not found: {key}") from exc
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
        except NotFound:
            pass

    # -- local staging -------------------------------------------------------

    def download_to_local(
        self,
        source: str,
        dest: Path | str,
    ) -> Path:
        """Download a GCS object to a local filesystem path.

        ``source`` may be either a plain key (resolved against this store's
        bucket and prefix via :meth:`_full_key`) or a fully-qualified
        ``gs://<bucket>/<key>`` URI. The latter is the common case for the
        ingest worker, which consumes URIs emitted by
        :meth:`materialize_table_input`.

        Cross-bucket ``gs://`` URIs are rejected: if the caller asks us to
        stage an object from a different bucket than the one this store was
        configured with, that's a configuration bug (e.g. staging worker
        accidentally reading production artifacts) and we want to surface it
        loudly rather than silently download.

        ``dest`` may be a path-like pointing to a file. Parent directories
        are created as needed. Returns the resolved ``Path``.
        """
        dest_path = Path(dest).expanduser().resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if source.startswith("gs://"):
            parsed = urlparse(source)
            if parsed.scheme != "gs":
                raise ValueError(f"Expected a gs:// URI, got: {source!r}")
            if parsed.netloc != self._bucket_name:
                raise ValueError(
                    f"Cannot download gs://{parsed.netloc}/... through a "
                    f"GcsArtifactStore configured for bucket "
                    f"{self._bucket_name!r}; cross-bucket access is a "
                    f"configuration error.",
                )
            blob_key = parsed.path.lstrip("/")
        else:
            blob_key = self._full_key(source)

        blob = self.bucket.blob(blob_key)
        t0 = time.perf_counter()
        self._with_retry(
            lambda: blob.download_to_filename(str(dest_path)),
            operation=f"download_to_local({blob_key})",
        )
        elapsed = time.perf_counter() - t0
        try:
            size_bytes = dest_path.stat().st_size
            mb = size_bytes / (1024 * 1024)
            rate = mb / elapsed if elapsed > 0 else 0
            logger.info(
                "Downloaded %.1f MB from gs://%s/%s in %.1fs (%.1f MB/s)",
                mb,
                self._bucket_name,
                blob_key,
                elapsed,
                rate,
            )
        except OSError:
            pass
        return dest_path

    # -- ingest checkpoints --------------------------------------------------

    def write_checkpoint(
        self,
        job_id: str,
        artifact_id: str,
        checkpoint: IngestCheckpoint,
        *,
        attempt_id: str = "",
        lease_generation: int | None = None,
    ) -> None:
        """Persist a monotonic ``IngestCheckpoint`` with GCS fencing."""
        key = f"jobs/{_safe_fragment(job_id)}/checkpoints/{_safe_fragment(artifact_id)}"
        blob = self.bucket.blob(self._full_key(key))
        expected_generation: int | None = None
        try:
            blob.reload()
            expected_generation = blob.generation
            existing_text = self._with_retry(
                lambda: blob.download_as_text(
                    encoding="utf-8",
                    if_generation_match=expected_generation,
                ),
                operation=f"read_checkpoint_for_update({key})",
            )
            existing_data = json.loads(existing_text)
            existing = IngestCheckpoint.model_validate(existing_data)
            if checkpoint.rows_committed < existing.rows_committed:
                raise ValueError(
                    f"Refusing non-monotonic checkpoint for job={job_id} "
                    f"artifact={artifact_id}: rows {checkpoint.rows_committed} "
                    f"< {existing.rows_committed}",
                )
            if checkpoint.chunks_committed < existing.chunks_committed:
                raise ValueError(
                    f"Refusing non-monotonic checkpoint for job={job_id} "
                    f"artifact={artifact_id}: chunks {checkpoint.chunks_committed} "
                    f"< {existing.chunks_committed}",
                )
        except NotFound:
            expected_generation = 0

        payload = checkpoint.model_copy(
            update={
                "attempt_id": attempt_id or checkpoint.attempt_id,
                "lease_generation": (
                    lease_generation
                    if lease_generation is not None
                    else checkpoint.lease_generation
                ),
            },
        ).model_dump(mode="json")
        self._with_retry(
            lambda: blob.upload_from_string(
                json.dumps(payload, ensure_ascii=False, default=str),
                content_type="application/json",
                if_generation_match=expected_generation,
            ),
            operation=f"write_checkpoint({key})",
        )

    def read_checkpoint(
        self,
        job_id: str,
        artifact_id: str,
    ) -> IngestCheckpoint | None:
        """Read an ``IngestCheckpoint`` from GCS, or ``None`` if absent."""
        key = f"jobs/{_safe_fragment(job_id)}/checkpoints/{_safe_fragment(artifact_id)}"
        try:
            data = self.get_json(key)
        except ArtifactNotFound:
            return None
        return IngestCheckpoint.model_validate(data)

    def delete_checkpoints(self, job_id: str) -> None:
        """Discard every recorded checkpoint for one job (no-op if absent).

        Exists for full re-runs only; the lease, not this, is what keeps a live
        attempt safe from concurrent writers.
        """
        prefix = self._full_key(f"jobs/{_safe_fragment(job_id)}/checkpoints/")
        for blob in self.bucket.client.list_blobs(self.bucket, prefix=prefix):
            self._with_retry(
                blob.delete,
                operation=f"delete_checkpoint({blob.name})",
            )

    # -- retry wrapper -------------------------------------------------------

    def _with_retry(self, fn, *, operation: str):
        started = time.perf_counter()
        attempt = 0
        while True:
            try:
                return fn()
            except Exception as exc:
                # HTTP 404 is a definitive "object does not exist" answer, not a
                # transient infra failure. Retrying only burns wall-clock time
                # while callers like PubSubWorkQueue.is_cancelled() wait for a
                # negative answer. Short-circuit before consulting the generic
                # retry policy so the decision is unambiguous regardless of how
                # the underlying SDK surfaces 404 (NotFound, InvalidResponse,
                # wrapped stringified error, etc.).
                if _is_not_found_error(exc):
                    raise
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


def _is_not_found_error(exc: BaseException) -> bool:
    """Return True when ``exc`` represents a hard HTTP 404.

    The GCS SDK can surface a 404 as either
    ``google.api_core.exceptions.NotFound`` (wrapped by the storage client) or
    ``google.resumable_media.common.InvalidResponse`` (when the underlying
    resumable-media download fails before the Storage client wraps it). We
    accept either, plus a defensive string fallback, so the caller never has
    to care which layer raised.
    """
    try:
        from google.api_core.exceptions import NotFound as _ApiNotFound

        if isinstance(exc, _ApiNotFound):
            return True
    except ImportError:
        pass
    try:
        from google.resumable_media.common import (
            InvalidResponse as _RmInvalidResponse,
        )

        if isinstance(exc, _RmInvalidResponse):
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status == 404:
                return True
    except ImportError:
        pass
    text = str(exc)
    return text.startswith("404 ") or "status code', 404," in text


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_datetime(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _lease_is_expired(record: LeaseRecord, *, grace_seconds: int) -> bool:
    try:
        expires_at = _parse_datetime(record.expires_at)
    except Exception:
        return True
    return _utc_now() >= expires_at + timedelta(seconds=max(int(grace_seconds), 0))


def _safe_fragment(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "artifact"
    return "".join(
        char if char.isalnum() or char in ("-", "_", "/") else "_" for char in text
    )
