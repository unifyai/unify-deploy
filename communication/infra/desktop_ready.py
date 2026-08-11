"""Publisher for the ``assistant_desktop_ready`` runtime event.

Desktop readiness reaches the assistant runtime as a Pub/Sub system event, and
two components need to emit it: the ``/infra/vm/ready`` endpoint (the VM pushes
its own readiness) and the session controller (which polls for readiness when
that push never lands). Both go through here so the wire shape stays identical
whichever side observes readiness first.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from common.settings import SETTINGS

from .runtime_clients import get_pubsub_clients

logger = logging.getLogger(__name__)


def publish_desktop_ready(
    assistant_id: str,
    hostname: str,
    vm_type: str,
    *,
    binding_id: str,
    desktop_secret: str | None = None,
) -> str:
    """Publish one ``assistant_desktop_ready`` inbound event; return its message id.

    The runtime's event handler derives the liveview URL (appending
    ``/desktop/custom.html``) and re-publishes to the thread Console's SSE
    subscription listens on, so this only carries the VM base URL.
    """
    publisher, _ = get_pubsub_clients()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = publisher.topic_path(SETTINGS.gcp_project_id, topic_name)

    event: dict[str, Any] = {
        "assistant_id": assistant_id,
        "binding_id": binding_id,
        "event_type": "assistant_desktop_ready",
        "desktop_url": f"https://{hostname}",
        "vm_type": vm_type,
        "message": f"VM ({vm_type}) startup complete",
    }
    if desktop_secret:
        event["desktop_secret"] = desktop_secret

    message_data = json.dumps(
        {
            "thread": "unity_system_event",
            "publish_timestamp": time.time(),
            "event": event,
        },
    ).encode("utf-8")

    future = publisher.publish(topic_path, data=message_data, thread="inbound")
    message_id = future.result()
    logger.info(
        f"Published assistant_desktop_ready for assistant {assistant_id} "
        f"(message_id={message_id})",
    )
    return message_id
