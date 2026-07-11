from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from unify.common.pipeline.retry_policy import ResilientRequestPolicy
from unify_deploy.infra.gcp.work_queue import PubSubWorkQueue


def _make_queue() -> tuple[PubSubWorkQueue, MagicMock]:
    settings = MagicMock()
    settings.project_id = "proj"
    settings.parse_topic = "parse-topic"
    settings.ingest_topic = "ingest-topic"
    settings.dead_letter_topic = "dlq-topic"
    settings.parse_subscription = "parse-sub"
    settings.ingest_subscription = "ingest-sub"
    settings.dead_letter_subscription = "dlq-sub"
    subscriber = MagicMock()
    queue = PubSubWorkQueue(
        publisher=MagicMock(),
        subscriber=subscriber,
        settings=settings,
        environment="production",
        retry_policy=ResilientRequestPolicy(max_retries=0),
    )
    return queue, subscriber


@pytest.mark.asyncio
async def test_receive_binds_receipt_to_source_subscription() -> None:
    queue, subscriber = _make_queue()
    message = SimpleNamespace(
        data=b'{"job_id":"job-1"}',
        message_id="pubsub-1",
        publish_time=None,
    )
    received = SimpleNamespace(
        ack_id="ack-1",
        message=message,
        delivery_attempt=3,
    )
    subscriber.pull.return_value = SimpleNamespace(received_messages=[received])

    items = await queue.receive(max_messages=1, topics=["ingest"])
    assert items[0].message_id == "pubsub-1"
    assert items[0].source_subscription == "projects/proj/subscriptions/ingest-sub"
    assert items[0].delivery_attempt == 3

    await queue.ack("ack-1")
    subscriber.acknowledge.assert_called_once_with(
        request={
            "subscription": "projects/proj/subscriptions/ingest-sub",
            "ack_ids": ["ack-1"],
        },
    )


@pytest.mark.asyncio
async def test_unknown_receipt_operations_fail_loudly() -> None:
    queue, _subscriber = _make_queue()

    with pytest.raises(RuntimeError, match="source subscription"):
        await queue.ack("missing")


@pytest.mark.asyncio
async def test_receive_supports_dead_letter_subscription_and_attributes() -> None:
    queue, subscriber = _make_queue()
    message = SimpleNamespace(
        data=b'{"kind":"ingest_requested","job_id":"job-1"}',
        message_id="dlq-pubsub-1",
        publish_time=None,
        attributes={"CloudPubSubDeadLetterSourceDeliveryCount": "5"},
    )
    received = SimpleNamespace(
        ack_id="dlq-ack-1",
        message=message,
        delivery_attempt=1,
    )
    subscriber.pull.return_value = SimpleNamespace(received_messages=[received])

    items = await queue.receive(max_messages=1, topics=["dead_letter"])

    assert items[0].source_subscription == "projects/proj/subscriptions/dlq-sub"
    assert (
        items[0].payload["_pubsub_attributes"][
            "CloudPubSubDeadLetterSourceDeliveryCount"
        ]
        == "5"
    )

    await queue.ack("dlq-ack-1")
    subscriber.acknowledge.assert_called_once_with(
        request={
            "subscription": "projects/proj/subscriptions/dlq-sub",
            "ack_ids": ["dlq-ack-1"],
        },
    )
