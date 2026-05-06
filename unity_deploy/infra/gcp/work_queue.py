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
        self._receipt_subscriptions: dict[str, str] = {}
        self._receipt_envelopes: dict[str, dict[str, Any]] = {}

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

    def _subscription_for_receipt(self, receipt_id: str) -> str:
        sub_path = self._receipt_subscriptions.get(receipt_id)
        if sub_path:
            return sub_path
        raise RuntimeError(
            "Cannot operate on Pub/Sub receipt without source subscription "
            f"binding: receipt={receipt_id!r}",
        )

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
                raw_payload = msg.message.data.decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw_payload)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = {}

                pubsub_message_id = str(getattr(msg.message, "message_id", "") or "")
                delivery_attempt = getattr(msg, "delivery_attempt", None)
                wq_msg = WorkQueueMessage(
                    message_id=pubsub_message_id or str(msg.ack_id),
                    topic=sub_key,
                    payload=payload,
                    published_at=(
                        msg.message.publish_time.isoformat()
                        if msg.message.publish_time
                        else utc_now_iso()
                    ),
                    pubsub_message_id=pubsub_message_id,
                    source_subscription=sub_path,
                    delivery_attempt=delivery_attempt,
                )
                item = ReceivedWorkItem(
                    **wq_msg.model_dump(),
                    receipt_id=msg.ack_id,
                    raw_payload=raw_payload,
                )
                self._receipt_subscriptions[msg.ack_id] = sub_path
                self._receipt_envelopes[msg.ack_id] = {
                    "topic": sub_key,
                    "source_subscription": sub_path,
                    "pubsub_message_id": pubsub_message_id,
                    "published_at": wq_msg.published_at,
                    "delivery_attempt": delivery_attempt,
                    "payload": payload,
                    "raw_payload": raw_payload,
                }
                items.append(item)

        return items

    async def ack(self, receipt_id: str) -> None:
        sub_path = self._subscription_for_receipt(receipt_id)
        await asyncio.to_thread(
            self._subscriber.acknowledge,
            request={
                "subscription": sub_path,
                "ack_ids": [receipt_id],
            },
        )
        self._receipt_subscriptions.pop(receipt_id, None)
        self._receipt_envelopes.pop(receipt_id, None)

    async def retry(
        self,
        receipt_id: str,
        *,
        error: str,
        delay_seconds: float = 0.0,
    ) -> None:
        new_deadline = max(int(delay_seconds), 0)
        sub_path = self._subscription_for_receipt(receipt_id)
        await asyncio.to_thread(
            self._subscriber.modify_ack_deadline,
            request={
                "subscription": sub_path,
                "ack_ids": [receipt_id],
                "ack_deadline_seconds": new_deadline,
            },
        )
        logger.info(
            "Retry scheduled (subscription=%s deadline=%ds, error=%s)",
            sub_path,
            new_deadline,
            error,
        )
        self._receipt_subscriptions.pop(receipt_id, None)
        self._receipt_envelopes.pop(receipt_id, None)

    async def dead_letter(self, receipt_id: str, *, error: str) -> None:
        envelope = self._receipt_envelopes.get(receipt_id, {})
        dead_letter_payload: dict[str, Any] = {
            "original_receipt_id": receipt_id,
            "error": error,
            "dead_lettered_at": utc_now_iso(),
            "original_topic": envelope.get("topic"),
            "source_subscription": envelope.get("source_subscription"),
            "pubsub_message_id": envelope.get("pubsub_message_id"),
            "published_at": envelope.get("published_at"),
            "delivery_attempt": envelope.get("delivery_attempt"),
            "payload": envelope.get("payload"),
        }
        await self.publish(topic="dead_letter", payload=dead_letter_payload)
        await self.ack(receipt_id)
        logger.warning("Dead-lettered message %s: %s", receipt_id, error)

    async def extend_lease(self, receipt_id: str, seconds: int) -> None:
        """Extend the Pub/Sub ack deadline for *receipt_id* by *seconds*.

        Called by the ``LeaseExtender`` while a handler is still
        processing a message, so Pub/Sub does not redeliver it to a
        second pod. ``modify_ack_deadline`` is the Pub/Sub API for both
        lease extension (positive new deadline) and early nack
        (``new_deadline=0``); here we always use it in extension mode.

        Errors are surfaced so queue lease failures are observable.
        """
        new_deadline = max(int(seconds), 1)
        sub_path = self._subscription_for_receipt(receipt_id)
        await asyncio.to_thread(
            self._subscriber.modify_ack_deadline,
            request={
                "subscription": sub_path,
                "ack_ids": [receipt_id],
                "ack_deadline_seconds": new_deadline,
            },
        )
        logger.debug(
            "extend_lease: subscription=%s receipt=%s deadline=%ds",
            sub_path,
            receipt_id,
            new_deadline,
        )

    def extend_lease_sync(self, receipt_id: str, seconds: int) -> None:
        """Synchronous ``modify_ack_deadline`` path for thread lease control."""
        new_deadline = max(int(seconds), 0)
        sub_path = self._subscription_for_receipt(receipt_id)
        self._subscriber.modify_ack_deadline(
            request={
                "subscription": sub_path,
                "ack_ids": [receipt_id],
                "ack_deadline_seconds": new_deadline,
            },
        )
        logger.debug(
            "extend_lease_sync: subscription=%s receipt=%s deadline=%ds",
            sub_path,
            receipt_id,
            new_deadline,
        )

    async def close(self) -> None:
        """Close Pub/Sub client channels and background transport threads.

        Called by workers during graceful shutdown after the main
        consumer loop has exited. ``SubscriberClient.close`` shuts down
        the gRPC channel and any in-flight streaming pull. The
        ``PublisherClient`` runs a background thread for batched
        publishes that must be stopped so the process can exit cleanly.

        Both calls are dispatched via ``asyncio.to_thread`` because the
        underlying GCP clients block. Errors are swallowed and logged
        so shutdown cannot itself hang or raise.
        """
        for client, method in (
            (self._subscriber, "close"),
            (self._publisher, "stop"),
        ):
            fn = getattr(client, method, None)
            if fn is None:
                continue
            try:
                await asyncio.to_thread(fn)
            except Exception:
                logger.exception(
                    "Failed to %s %s during shutdown",
                    method,
                    type(client).__name__,
                )

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
