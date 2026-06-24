"""Best-effort worker event journal helpers."""

from __future__ import annotations

import logging
import os
from typing import Any

from unity.common.pipeline.work_queue import ReceivedWorkItem

from unity_deploy.infra.gcp.pipeline_observability import (
    PipelineJobEvent,
    classify_error,
    make_receipt_hash,
    payload_dispatch_id,
    payload_job_id,
    topic_for_payload,
    write_job_event,
)

logger = logging.getLogger(__name__)


def record_worker_event(
    infra: Any,
    item: ReceivedWorkItem | None = None,
    *,
    event_type: str,
    job_id: str = "",
    dispatch_id: str = "",
    stage: str = "",
    error: str = "",
    next_action: str = "",
    metadata: dict[str, Any] | None = None,
    rows_committed: int | None = None,
    chunks_committed: int | None = None,
    table_id: str = "",
    attempt_id: str = "",
) -> None:
    try:
        payload = item.payload if item is not None else {}
        event = PipelineJobEvent(
            event_type=event_type,
            environment=getattr(infra.settings, "environment", ""),
            project_id=getattr(infra.settings.pubsub, "project_id", ""),
            job_id=job_id or payload_job_id(payload or {}),
            dispatch_id=dispatch_id or payload_dispatch_id(payload or {}),
            stage=stage or topic_for_payload(payload or {}),
            table_id=table_id,
            attempt_id=attempt_id,
            pubsub_message_id=(
                (item.pubsub_message_id or item.message_id) if item is not None else ""
            ),
            delivery_attempt=item.delivery_attempt if item is not None else None,
            receipt_hash=make_receipt_hash(item.receipt_id) if item is not None else "",
            source_subscription=item.source_subscription if item is not None else "",
            worker_pod=os.environ.get("HOSTNAME", ""),
            rows_committed=rows_committed,
            chunks_committed=chunks_committed,
            error_type=type(error).__name__ if error else "",
            error_message=error,
            retry_classification=classify_error(error) if error else "",
            next_action=next_action,
            metadata=metadata or {},
        )
        if event.job_id:
            write_job_event(infra.artifact_store, event)
    except Exception:
        logger.exception("Failed to write pipeline job event: %s", event_type)
