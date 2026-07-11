"""Durable observability and recovery helpers for pipeline jobs.

This module is intentionally environment-agnostic.  It stores full resource
paths and derives concrete Pub/Sub/GCS names from ``GcpPipelineSettings`` via
the existing ``GcsArtifactStore`` and ``PubSubWorkQueue`` adapters.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from unify.common.pipeline._utils import utc_now_iso
from unify.common.pipeline.types import IngestCheckpoint
from unify.common.pipeline.work_queue import ReceivedWorkItem

from .artifact_store import GcsArtifactStore, LeaseRecord

QueueStage = Literal["parse", "ingest", "dead_letter", "unknown"]
RetryClassification = Literal[
    "retryable",
    "operator_retryable",
    "defer_active",
    "needs_operator",
    "non_retryable",
]


class PipelineQueueIdentity(BaseModel):
    """Stable identity extracted from any current or legacy queue payload."""

    job_id: str = ""
    dispatch_id: str = ""
    kind: str = ""
    topic: QueueStage = "unknown"
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = ""


def safe_fragment(value: str) -> str:
    text = str(value or "").strip() or "unknown"
    return "".join(
        char if char.isalnum() or char in ("-", "_") else "_" for char in text
    )


def event_sort_key(timestamp: str, event_id: str = "") -> str:
    raw = f"{timestamp}-{event_id or uuid4().hex}"
    return safe_fragment(raw.replace(":", "").replace("+", "Z"))


def extract_queue_identity(payload: dict[str, Any]) -> PipelineQueueIdentity:
    """Extract job/dispatch/stage identity from queue or DLQ payloads.

    Supported shapes:
    - native/current ``ParseRequested`` and ``IngestRequested`` payloads
    - app-level DLQ wrappers containing ``payload`` and ``original_topic``
    - persisted DLQ records containing top-level ``job_id`` / ``dispatch_id``
    - legacy app-level DLQ wrappers that only preserved ``error=... job=<id>``
    """
    outer = dict(payload or {})
    inner = outer.get("payload") if isinstance(outer.get("payload"), dict) else outer
    inner = dict(inner or {})
    job_id = str(outer.get("job_id") or inner.get("job_id") or "")
    dispatch_id = str(outer.get("dispatch_id") or inner.get("dispatch_id") or "")
    kind = str(inner.get("kind") or outer.get("kind") or "")
    original_topic = str(
        outer.get("original_topic") or inner.get("original_topic") or "",
    )
    if kind == "parse_requested":
        topic: QueueStage = "parse"
    elif kind == "ingest_requested":
        topic = "ingest"
    elif original_topic in {"parse", "ingest"}:
        topic = original_topic  # type: ignore[assignment]
    else:
        topic = "unknown"
    if not job_id:
        error = str(outer.get("error") or inner.get("error") or "")
        match = re.search(r"\bjob=([A-Za-z0-9_-]+)", error)
        if match:
            job_id = match.group(1)
    source = "direct"
    if isinstance(outer.get("payload"), dict):
        source = "app_dlq_wrapper"
    if not kind and job_id and source == "direct":
        source = "durable_or_legacy"
    recovered_payload = dict(inner)
    recovered_payload.pop("_pubsub_attributes", None)
    return PipelineQueueIdentity(
        job_id=job_id,
        dispatch_id=dispatch_id,
        kind=kind,
        topic=topic,
        payload=recovered_payload,
        source=source,
    )


def payload_job_id(payload: dict[str, Any]) -> str:
    return extract_queue_identity(payload).job_id


def payload_dispatch_id(payload: dict[str, Any]) -> str:
    return extract_queue_identity(payload).dispatch_id


def payload_kind(payload: dict[str, Any]) -> str:
    return extract_queue_identity(payload).kind


def topic_for_payload(payload: dict[str, Any]) -> QueueStage:
    return extract_queue_identity(payload).topic


def classify_error(error: str | None) -> RetryClassification:
    text = (error or "").lower()
    if not text:
        return "operator_retryable"
    non_retryable = (
        "table config contains entries",
        "row-count",
        "row count",
        "missing manifest",
        "manifest missing",
        "invalid payload",
        "expected_total_rows",
        "schema",
    )
    if any(marker in text for marker in non_retryable):
        return "non_retryable"
    needs_operator = (
        "permission",
        "unauthorized",
        "forbidden",
        "api key",
        "assistant key",
        "authentication",
    )
    if any(marker in text for marker in needs_operator):
        return "needs_operator"
    retryable = (
        "deadline",
        "timeout",
        "temporar",
        "unavailable",
        "connection",
        "lease",
        "429",
        "500",
        "502",
        "503",
        "504",
    )
    if any(marker in text for marker in retryable):
        return "retryable"
    return "operator_retryable"


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_fresh_timestamp(value: str | None, *, max_age_seconds: int) -> bool:
    parsed = parse_timestamp(value)
    if parsed is None:
        return False
    age = (datetime.now(timezone.utc) - parsed).total_seconds()
    return age <= max_age_seconds


def is_fresh_lease(lease: LeaseRecord) -> bool:
    expires = parse_timestamp(lease.expires_at)
    return expires is not None and expires > datetime.now(timezone.utc)


class PipelineDlqRecord(BaseModel):
    record_type: Literal["dlq"] = "dlq"
    dlq_record_id: str = Field(default_factory=lambda: uuid4().hex)
    environment: str = ""
    project_id: str = ""
    job_id: str = ""
    dispatch_id: str = ""
    original_topic: QueueStage = "unknown"
    retry_topic: QueueStage = "unknown"
    dlq_subscription: str = ""
    source_subscription: str = ""
    pubsub_message_id: str = ""
    dlq_message_id: str = ""
    source_topic_publish_time: str = ""
    dlq_publish_time: str = ""
    delivery_attempt: int | None = None
    error: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    raw_payload: str = ""
    checkpoint_snapshot: dict[str, Any] = Field(default_factory=dict)
    previous_job_status: str = ""
    retry_classification: RetryClassification = "operator_retryable"
    recorded_at: str = Field(default_factory=utc_now_iso)


class PipelineJobEvent(BaseModel):
    record_type: Literal["event"] = "event"
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    event_type: str
    environment: str = ""
    project_id: str = ""
    job_id: str = ""
    dispatch_id: str = ""
    stage: QueueStage | str = "unknown"
    table_id: str = ""
    attempt_id: str = ""
    pubsub_message_id: str = ""
    delivery_attempt: int | None = None
    receipt_hash: str = ""
    source_subscription: str = ""
    worker_pod: str = ""
    rows_committed: int | None = None
    chunks_committed: int | None = None
    error_type: str = ""
    error_message: str = ""
    retry_classification: RetryClassification | str = ""
    next_action: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    recorded_at: str = Field(default_factory=utc_now_iso)


class JobObservabilitySnapshot(BaseModel):
    job_id: str
    dispatch_id: str = ""
    durable_status: str = "unknown"
    derived_status: str = "unknown"
    source_file: str = ""
    target_context: str = ""
    last_event_at: str = ""
    latest_heartbeat_at: str = ""
    latest_checkpoint_rows: int = 0
    latest_checkpoint_chunks: int = 0
    latest_checkpoint_table: str = ""
    active_lease_owner: str = ""
    active_lease_expires_at: str = ""
    queue_location: str = "none"
    delivery_attempt: int | None = None
    retry_classification: RetryClassification | str = ""
    retry_eligible: bool = False
    next_action: str = ""
    status_reason: str = ""
    latest_heartbeat_age_seconds: int | None = None
    checkpoint_complete: bool | None = None
    retry_payload_source: str = ""
    recovery_action: str = ""
    dlq_records: list[PipelineDlqRecord] = Field(default_factory=list)
    checkpoints: dict[str, IngestCheckpoint] = Field(default_factory=dict)
    events: list[PipelineJobEvent] = Field(default_factory=list)


def make_receipt_hash(receipt_id: str) -> str:
    if not receipt_id:
        return ""
    return hashlib.sha256(receipt_id.encode("utf-8")).hexdigest()[:12]


def dlq_record_from_received_item(
    item: ReceivedWorkItem,
    *,
    environment: str,
    project_id: str,
    dlq_subscription: str,
    checkpoint_snapshot: dict[str, Any] | None = None,
    previous_job_status: str = "",
) -> PipelineDlqRecord:
    payload = dict(item.payload or {})
    wrapped_payload = (
        payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    )
    attrs = dict(payload.get("_pubsub_attributes") or {})
    source_subscription = (
        attrs.get("CloudPubSubDeadLetterSourceSubscription")
        or payload.get("source_subscription")
        or item.source_subscription
    )
    delivery_attempt = (
        attrs.get("CloudPubSubDeadLetterSourceDeliveryCount")
        or payload.get("delivery_attempt")
        or item.delivery_attempt
    )
    try:
        delivery_attempt = (
            int(delivery_attempt) if delivery_attempt is not None else None
        )
    except (TypeError, ValueError):
        delivery_attempt = None
    error = payload.get("error")
    identity = extract_queue_identity(payload)
    retry_topic = identity.topic
    recovered_payload = identity.payload
    return PipelineDlqRecord(
        environment=environment,
        project_id=project_id,
        job_id=identity.job_id,
        dispatch_id=identity.dispatch_id,
        original_topic=retry_topic,
        retry_topic=retry_topic,
        dlq_subscription=dlq_subscription,
        source_subscription=str(source_subscription or ""),
        pubsub_message_id=str(payload.get("pubsub_message_id") or ""),
        dlq_message_id=item.pubsub_message_id or item.message_id,
        source_topic_publish_time=str(
            attrs.get("CloudPubSubDeadLetterSourceTopicPublishTime")
            or payload.get("published_at")
            or "",
        ),
        dlq_publish_time=item.published_at,
        delivery_attempt=delivery_attempt,
        error=str(error) if error else None,
        payload=recovered_payload,
        raw_payload=item.raw_payload,
        checkpoint_snapshot=checkpoint_snapshot or {},
        previous_job_status=previous_job_status,
        retry_classification=classify_error(str(error) if error else None),
    )


def write_job_event(store: GcsArtifactStore, event: PipelineJobEvent) -> str:
    key = (
        f"jobs/{safe_fragment(event.job_id)}/events/"
        f"{event_sort_key(event.recorded_at, event.event_id)}.json"
    )
    store.put_json(key, event.model_dump(mode="json"), if_generation_match=0)
    return key


def write_dlq_record(store: GcsArtifactStore, record: PipelineDlqRecord) -> list[str]:
    message_key = safe_fragment(record.dlq_message_id or record.dlq_record_id)
    keys = [
        f"jobs/{safe_fragment(record.job_id)}/dlq/{message_key}.json",
    ]
    if record.dispatch_id:
        keys.append(
            "dispatches/"
            f"{safe_fragment(record.dispatch_id)}/dlq/"
            f"{safe_fragment(record.job_id)}-{message_key}.json",
        )
    written: list[str] = []
    payload = record.model_dump(mode="json")
    for key in keys:
        try:
            store.put_json(key, payload, if_generation_match=0)
        except Exception:
            if not store.exists(key):
                raise
        written.append(key)
    return written


def _logical_key(store: GcsArtifactStore, blob_name: str) -> str:
    prefix = store._full_key("")
    if prefix and blob_name.startswith(prefix):
        return blob_name[len(prefix) :].lstrip("/")
    return blob_name


def list_json_records(
    store: GcsArtifactStore,
    logical_prefix: str,
    model: type[PipelineDlqRecord] | type[PipelineJobEvent],
) -> list[Any]:
    full_prefix = store._full_key(logical_prefix)
    records: list[Any] = []
    for blob in store.bucket.list_blobs(prefix=full_prefix):
        if not blob.name.endswith(".json"):
            continue
        try:
            data = json.loads(blob.download_as_text(encoding="utf-8"))
            records.append(model.model_validate(data))
        except Exception:
            continue
    records.sort(key=lambda r: getattr(r, "recorded_at", ""))
    return records


def list_job_dlq_records(
    store: GcsArtifactStore,
    job_id: str,
) -> list[PipelineDlqRecord]:
    return list_json_records(
        store,
        f"jobs/{safe_fragment(job_id)}/dlq/",
        PipelineDlqRecord,
    )


def list_dispatch_dlq_records(
    store: GcsArtifactStore,
    dispatch_id: str,
) -> list[PipelineDlqRecord]:
    return list_json_records(
        store,
        f"dispatches/{safe_fragment(dispatch_id)}/dlq/",
        PipelineDlqRecord,
    )


def list_job_events(store: GcsArtifactStore, job_id: str) -> list[PipelineJobEvent]:
    return list_json_records(
        store,
        f"jobs/{safe_fragment(job_id)}/events/",
        PipelineJobEvent,
    )


def list_job_checkpoints(
    store: GcsArtifactStore,
    job_id: str,
) -> dict[str, IngestCheckpoint]:
    full_prefix = store._full_key(f"jobs/{safe_fragment(job_id)}/checkpoints/")
    checkpoints: dict[str, IngestCheckpoint] = {}
    for blob in store.bucket.list_blobs(prefix=full_prefix):
        try:
            data = json.loads(blob.download_as_text(encoding="utf-8"))
            checkpoint = IngestCheckpoint.model_validate(data)
            checkpoints[checkpoint.artifact_id] = checkpoint
        except Exception:
            continue
    return checkpoints


def latest_heartbeat_at(store: GcsArtifactStore, job_id: str) -> str:
    key = f"jobs/{safe_fragment(job_id)}/heartbeats.jsonl"
    try:
        blob = store.bucket.blob(store._full_key(key))
        if not blob.exists():
            return ""
        lines = [
            ln for ln in blob.download_as_text(encoding="utf-8").splitlines() if ln
        ]
        if not lines:
            return ""
        data = json.loads(lines[-1])
        return str(data.get("recorded_at") or data.get("timestamp") or "")
    except Exception:
        return ""


def read_active_leases(store: GcsArtifactStore, job_id: str) -> list[LeaseRecord]:
    full_prefix = store._full_key(f"jobs/{safe_fragment(job_id)}/leases/")
    leases: list[LeaseRecord] = []
    for blob in store.bucket.list_blobs(prefix=full_prefix):
        if not blob.name.endswith(".json"):
            continue
        try:
            data = json.loads(blob.download_as_text(encoding="utf-8"))
            leases.append(LeaseRecord(**data, generation=blob.generation))
        except Exception:
            continue
    return leases


def queued_stale_age_seconds() -> int:
    """Min seconds a job may sit ``durable=queued`` before it is recoverable.

    Guards against prematurely "recovering" a freshly enqueued job whose ingest
    message is still in flight. Tunable via ``UNITY_QUEUED_STALE_AGE_SECONDS``.
    """
    try:
        return int(os.environ.get("UNITY_QUEUED_STALE_AGE_SECONDS", "900"))
    except ValueError:
        return 900


def derive_status(
    *,
    durable_status: str,
    dlq_records: list[PipelineDlqRecord],
    checkpoints: dict[str, IngestCheckpoint],
    heartbeat_at: str = "",
    leases: list[LeaseRecord] | None = None,
    queued_at: str = "",
) -> tuple[str, str, bool]:
    if durable_status in {"success", "cancelled", "paused"}:
        return (
            durable_status,
            "terminal" if durable_status == "success" else durable_status,
            False,
        )
    fresh_lease = any(is_fresh_lease(lease) for lease in leases or [])
    fresh_heartbeat = is_fresh_timestamp(heartbeat_at, max_age_seconds=600)
    if fresh_heartbeat or fresh_lease:
        return "running-active", "active", False
    if dlq_records:
        classification = dlq_records[-1].retry_classification
        if checkpoints:
            return "partial-dlq", classification, classification != "non_retryable"
        return "dlq", classification, classification != "non_retryable"
    if durable_status == "error":
        return "error", "needs_operator", False
    if durable_status == "running":
        return "running-stale", "operator_retryable", True
    if durable_status == "queued":
        # A job stuck in durable=queued with no fresh heartbeat/lease is either
        # (a) freshly enqueued with its ingest message still in flight, or
        # (b) limbo: the message was lost during pause/resume/rollout churn and
        # no worker will ever pick it up. Distinguish by queued-age — only
        # classify as recoverable once it has sat queued past the guard window,
        # so the 15-min reconcile cron can self-heal limbo without racing a
        # just-dispatched job's in-flight message.
        if not queued_at or is_fresh_timestamp(
            queued_at,
            max_age_seconds=queued_stale_age_seconds(),
        ):
            return "queued", "queued", False
        return "queued-stale", "operator_retryable", True
    return durable_status or "unknown", "unknown", False


def checkpoint_snapshot(checkpoints: dict[str, IngestCheckpoint]) -> dict[str, Any]:
    return {
        artifact_id: checkpoint.model_dump(mode="json")
        for artifact_id, checkpoint in checkpoints.items()
    }
