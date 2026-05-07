from __future__ import annotations

from unity.common.pipeline.types import IngestCheckpoint
from unity.common.pipeline.work_queue import ReceivedWorkItem

from unity_deploy.infra.gcp.pipeline_observability import (
    classify_error,
    derive_status,
    dlq_record_from_received_item,
)


def test_native_dlq_record_preserves_source_metadata() -> None:
    item = ReceivedWorkItem(
        message_id="dlq-msg-1",
        topic="dead_letter",
        payload={
            "kind": "ingest_requested",
            "job_id": "job-1",
            "dispatch_id": "dispatch-1",
            "_pubsub_attributes": {
                "CloudPubSubDeadLetterSourceSubscription": (
                    "projects/proj/subscriptions/unity-ingest-sub-staging"
                ),
                "CloudPubSubDeadLetterSourceDeliveryCount": "5",
            },
        },
        receipt_id="ack-1",
        pubsub_message_id="dlq-msg-1",
        source_subscription="projects/proj/subscriptions/unity-dead-letter-sub-staging",
        raw_payload='{"kind":"ingest_requested","job_id":"job-1"}',
    )

    record = dlq_record_from_received_item(
        item,
        environment="staging",
        project_id="proj",
        dlq_subscription="projects/proj/subscriptions/unity-dead-letter-sub-staging",
        previous_job_status="running",
    )

    assert record.job_id == "job-1"
    assert record.dispatch_id == "dispatch-1"
    assert record.retry_topic == "ingest"
    assert record.source_subscription.endswith("unity-ingest-sub-staging")
    assert record.delivery_attempt == 5
    assert record.previous_job_status == "running"


def test_app_dead_letter_record_uses_wrapped_payload_and_error() -> None:
    item = ReceivedWorkItem(
        message_id="dlq-msg-2",
        topic="dead_letter",
        payload={
            "original_topic": "parse",
            "error": "connection timeout",
            "delivery_attempt": 3,
            "payload": {
                "kind": "parse_requested",
                "job_id": "job-2",
                "dispatch_id": "dispatch-2",
            },
        },
        receipt_id="ack-2",
        pubsub_message_id="dlq-msg-2",
        source_subscription="projects/proj/subscriptions/unity-dead-letter-sub",
    )

    record = dlq_record_from_received_item(
        item,
        environment="production",
        project_id="proj",
        dlq_subscription="projects/proj/subscriptions/unity-dead-letter-sub",
    )

    assert record.job_id == "job-2"
    assert record.retry_topic == "parse"
    assert record.retry_classification == "retryable"
    assert record.error == "connection timeout"


def test_running_with_dlq_and_checkpoint_derives_partial_dlq() -> None:
    derived, classification, retryable = derive_status(
        durable_status="running",
        dlq_records=[
            dlq_record_from_received_item(
                ReceivedWorkItem(
                    message_id="dlq-msg-3",
                    topic="dead_letter",
                    payload={
                        "kind": "ingest_requested",
                        "job_id": "job-3",
                        "dispatch_id": "dispatch-3",
                    },
                    receipt_id="ack-3",
                ),
                environment="staging",
                project_id="proj",
                dlq_subscription="projects/proj/subscriptions/dlq",
            ),
        ],
        checkpoints={
            "table-1": IngestCheckpoint(
                job_id="job-3",
                artifact_id="table-1",
                rows_committed=100,
                chunks_committed=2,
            ),
        },
    )

    assert derived == "partial-dlq"
    assert classification == "operator_retryable"
    assert retryable is True


def test_retry_policy_classification_bounds_deterministic_errors() -> None:
    assert (
        classify_error("table config contains entries that did not match")
        == "non_retryable"
    )
    assert classify_error("Permission denied for service account") == "needs_operator"
    assert classify_error("503 unavailable") == "retryable"
