#!/usr/bin/env python3
"""Publish assistant_desktop_ready to the local Pub/Sub emulator."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def publish_desktop_ready(
    *,
    assistant_id: int,
    desktop_url: str,
    binding_id: str,
    vm_type: str,
    project_id: str,
    emulator_host: str,
) -> str:
    from google.cloud import pubsub_v1

    os.environ["PUBSUB_EMULATOR_HOST"] = emulator_host
    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(project_id, f"unity-{assistant_id}-staging")

    payload = {
        "thread": "unity_system_event",
        "publish_timestamp": time.time(),
        "event": {
            "assistant_id": str(assistant_id),
            "binding_id": binding_id,
            "event_type": "assistant_desktop_ready",
            "desktop_url": desktop_url.rstrip("/"),
            "vm_type": vm_type,
            "message": "Self-host desktop ready",
        },
    }
    data = json.dumps(payload).encode("utf-8")
    future = publisher.publish(topic_path, data, thread="inbound")
    return future.result()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assistant-id", type=int, required=True)
    parser.add_argument(
        "--desktop-url",
        default=os.environ.get("SELF_HOST_DESKTOP_URL", "http://127.0.0.1:8090"),
    )
    parser.add_argument("--binding-id", default="self-host-local")
    parser.add_argument("--vm-type", default="ubuntu")
    parser.add_argument(
        "--project-id",
        default=os.environ.get("GCP_PROJECT_ID", "local-test-project"),
    )
    parser.add_argument(
        "--emulator-host",
        default=os.environ.get("PUBSUB_EMULATOR_HOST", "localhost:8085"),
    )
    args = parser.parse_args()

    try:
        message_id = publish_desktop_ready(
            assistant_id=args.assistant_id,
            desktop_url=args.desktop_url,
            binding_id=args.binding_id,
            vm_type=args.vm_type,
            project_id=args.project_id,
            emulator_host=args.emulator_host,
        )
    except Exception as exc:
        print(f"Failed to publish assistant_desktop_ready: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"message_id": message_id, "desktop_url": args.desktop_url}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
