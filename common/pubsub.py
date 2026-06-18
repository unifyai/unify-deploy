"""Shared Pub/Sub publisher for the Communication service.

Centralises the ``droid-{assistant_id}{env_suffix}`` topic convention so
both the FastAPI service (``communication/...``) and the adapters Cloud
Functions (``adapters/helpers.py``) emit identical envelopes.

Envelope schema
---------------

Every message body is::

    {
        "thread":            "<thread>",         # e.g. "teams_meet"
        "publish_timestamp": <unix-seconds>,
        "event":             { ...payload... },
    }

Pub/Sub attributes
------------------

A single ``thread`` attribute is set on the Pub/Sub message so
subscribers can filter server-side when needed (Droid's comms_manager
reads the body, but future filters / Cloud Tasks can use the
attribute).  The ``thread`` attribute value matches the one inside the
body.

Why a thin wrapper?
-------------------

The Python ``PublisherClient`` is expensive to construct (underlying
gRPC channel) and the same singleton is reused for the lifetime of the
process; lazy construction avoids import-time GCP credential side
effects in tests.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping, Optional

from google.cloud import pubsub_v1

from common.settings import SETTINGS

logger = logging.getLogger(__name__)

_publisher: Optional[pubsub_v1.PublisherClient] = None


def get_pubsub_publisher() -> pubsub_v1.PublisherClient:
    """Return (and lazily construct) the module-level publisher client."""
    global _publisher
    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def publish_assistant_event(
    *,
    assistant_id: str,
    thread: str,
    event: Mapping[str, Any],
    timeout_s: float = 10.0,
) -> str:
    """Publish an event envelope to ``droid-{assistant_id}{env_suffix}``.

    Returns the Pub/Sub message ID.  Raises if the publish future fails
    — callers that treat publish failures as non-fatal should wrap in
    ``try/except``.
    """
    publisher = get_pubsub_publisher()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = publisher.topic_path(SETTINGS.gcp_project_id, topic_name)

    envelope = {
        "thread": thread,
        "publish_timestamp": time.time(),
        "event": dict(event),
    }
    data = json.dumps(envelope).encode("utf-8")

    future = publisher.publish(topic_path, data=data, thread=thread)
    message_id = future.result(timeout=timeout_s)
    logger.info(
        "Published %s event for assistant %s to %s (msg_id=%s)",
        thread,
        assistant_id,
        topic_path,
        message_id,
    )
    return message_id
