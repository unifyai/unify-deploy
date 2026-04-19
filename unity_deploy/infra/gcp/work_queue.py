"""Pub/Sub-backed implementation of the WorkQueue protocol."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from google.cloud import pubsub_v1

from unity.common.pipeline._utils import utc_now_iso
from unity.common.pipeline.artifact_store import ArtifactStore
from unity.common.pipeline.retry_policy import ResilientRequestPolicy
from unity.common.pipeline.work_queue import (
    ReceivedWorkItem,
    WorkQueueMessage,
)

from .settings import PubSubQueueSettings

logger = logging.getLogger(__name__)


class PubSubWorkQueue:
    """Pub/Sub-backed work queue implementing the unity WorkQueue protocol.

    All blocking GCP client calls are dispatched via
    ``asyncio.to_thread`` so the event loop is never blocked.

    Cancellation is checked via the artifact store (GCS read on the
    job record), not via Pub/Sub itself.
    """

    def __init__(
        self,
        *,
        publisher: pubsub_v1.PublisherClient,
        subscriber: pubsub_v1.SubscriberClient,
        settings: PubSubQueueSettings,
        environment: str = "staging",
        retry_policy: ResilientRequestPolicy | None = None,
        cancellation_store: ArtifactStore | None = None,
    ):
        self._publisher = publisher
        self._subscriber = subscriber
        self._settings = settings
        self._environment = environment
        self._retry_policy = retry_policy or ResilientRequestPolicy()
        self._cancellation_store = cancellation_store

        self._topic_map: dict[str, str] = {
            "parse": self._full_topic(settings.parse_topic),
            "ingest": self._full_topic(settings.ingest_topic),
            "dead_letter": self._full_topic(settings.dead_letter_topic),
        }
        self._sub_map: dict[str, str] = {
            "parse": self._full_subscription(settings.parse_subscription),
            "ingest": self._full_subscription(settings.ingest_subscription),
        }

    def _env_suffix(self) -> str:
        """Return the environment suffix used in shared resource names.

        Matches the convention used across unity/communication:
        production -> no suffix; other envs -> ``-{env}``.
        """
        return "" if self._environment == "production" else f"-{self._environment}"

    def _full_topic(self, base: str) -> str:
        return f"projects/{self._settings.project_id}/topics/{base}{self._env_suffix()}"

    def _full_subscription(self, base: str) -> str:
        return f"projects/{self._settings.project_id}/subscriptions/{base}{self._env_suffix()}"

    # -- WorkQueue protocol --------------------------------------------------

    async def publish(self, *, topic: str, payload: dict[str, Any]) -> str:
        topic_path = self._topic_map.get(topic) or self._full_topic(topic)
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

        def _sync_publish() -> str:
            future = self._publisher.publish(topic_path, data=data)
            return str(future.result(timeout=30))

        message_id = await asyncio.to_thread(_sync_publish)
        logger.info("Published to %s: message_id=%s", topic, message_id)
        return message_id

    async def receive(
        self,
        *,
        max_messages: int = 1,
        topics: list[str] | None = None,
    ) -> list[ReceivedWorkItem]:
        sub_names = list(topics or ["parse", "ingest"])
        items: list[ReceivedWorkItem] = []

        for sub_key in sub_names:
            if len(items) >= max_messages:
                break
            sub_path = self._sub_map.get(sub_key)
            if sub_path is None:
                continue

            remaining = max_messages - len(items)
            response = await asyncio.to_thread(
                self._subscriber.pull,
                request={
                    "subscription": sub_path,
                    "max_messages": remaining,
                },
                timeout=30,
            )

            for msg in response.received_messages:
                try:
                    payload = json.loads(msg.message.data.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = {}

                wq_msg = WorkQueueMessage(
                    topic=sub_key,
                    payload=payload,
                    published_at=(
                        msg.message.publish_time.isoformat()
                        if msg.message.publish_time
                        else utc_now_iso()
                    ),
                )
                item = ReceivedWorkItem(
                    **wq_msg.model_dump(),
                    receipt_id=msg.ack_id,
                )
                items.append(item)

        return items

    async def ack(self, receipt_id: str) -> None:
        for sub_path in self._sub_map.values():
            try:
                await asyncio.to_thread(
                    self._subscriber.acknowledge,
                    request={
                        "subscription": sub_path,
                        "ack_ids": [receipt_id],
                    },
                )
                return
            except Exception:
                continue

    async def retry(
        self,
        receipt_id: str,
        *,
        error: str,
        delay_seconds: float = 0.0,
    ) -> None:
        new_deadline = max(int(delay_seconds), 0)
        for sub_path in self._sub_map.values():
            try:
                await asyncio.to_thread(
                    self._subscriber.modify_ack_deadline,
                    request={
                        "subscription": sub_path,
                        "ack_ids": [receipt_id],
                        "ack_deadline_seconds": new_deadline,
                    },
                )
                logger.info(
                    "Retry scheduled (deadline=%ds, error=%s)",
                    new_deadline,
                    error,
                )
                return
            except Exception:
                continue

    async def dead_letter(self, receipt_id: str, *, error: str) -> None:
        dead_letter_payload: dict[str, Any] = {
            "original_receipt_id": receipt_id,
            "error": error,
            "dead_lettered_at": utc_now_iso(),
        }
        await self.publish(topic="dead_letter", payload=dead_letter_payload)
        await self.ack(receipt_id)
        logger.warning("Dead-lettered message %s: %s", receipt_id, error)

    async def is_cancelled(self, run_id: str) -> bool:
        if self._cancellation_store is None:
            return False

        def _sync_check() -> bool:
            try:
                job_data = self._cancellation_store.get_json(  # type: ignore[union-attr]
                    f"jobs/{run_id}/job.json",
                )
                return job_data.get("status") == "cancelled"
            except Exception:
                return False

        return await asyncio.to_thread(_sync_check)
