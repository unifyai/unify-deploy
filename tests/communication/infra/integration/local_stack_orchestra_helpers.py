"""Shared Orchestra and Pub/Sub helpers for local-stack integration tests."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid

import requests
from google.api_core.exceptions import DeadlineExceeded, NotFound

from tests.communication.infra.integration.conftest import LocalStackUrls

_TEMP_SUBSCRIPTION_TTL_SECONDS = 86_400


def unify_key() -> str:
    return os.environ["UNIFY_KEY"]


def admin_key() -> str:
    return os.environ["ORCHESTRA_ADMIN_KEY"]


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def create_organization(urls: LocalStackUrls, name: str) -> dict:
    response = requests.post(
        f"{urls.orchestra_url}/organizations",
        json={"name": name},
        headers=auth_headers(unify_key()),
        timeout=30,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    org_api_key = body["api_key"]
    return {
        "id": body["id"],
        "api_key": org_api_key,
        "headers": auth_headers(org_api_key),
    }


def create_team(urls: LocalStackUrls, org: dict, name: str) -> dict:
    response = requests.post(
        f"{urls.orchestra_url}/organizations/{org['id']}/teams",
        json={"name": name, "description": f"{name} shared team"},
        headers=org["headers"],
        timeout=30,
    )
    assert response.status_code == 201, response.text
    return response.json()


def delete_team(urls: LocalStackUrls, org: dict, team_id: int) -> None:
    requests.delete(
        f"{urls.orchestra_url}/organizations/{org['id']}/teams/{team_id}",
        headers=org["headers"],
        timeout=30,
    )


def list_org_assistants(urls: LocalStackUrls, org: dict) -> list[dict]:
    response = requests.get(
        f"{urls.orchestra_url}/assistant",
        headers=org["headers"],
        timeout=30,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body.get("info", body)


def fetch_admin_assistant_record(urls: LocalStackUrls, assistant_id: str) -> dict:
    response = requests.get(
        f"{urls.orchestra_url}/admin/assistant",
        params={"agent_id": assistant_id},
        headers=auth_headers(admin_key()),
        timeout=30,
    )
    assert response.status_code == 200, response.text
    info = response.json()["info"]
    return info[0] if isinstance(info, list) else info


def org_coordinator(urls: LocalStackUrls, org: dict) -> dict:
    assistants = list_org_assistants(urls, org)
    return next(
        assistant for assistant in assistants if assistant.get("is_coordinator")
    )


def mark_assistant_local_runtime(assistant_id: str) -> None:
    """Mark an Orchestra assistant as using a caller-local Unity runtime."""

    db_container = os.getenv("ORCHESTRA_DB_CONTAINER", "orchestra-local-db")
    completed = subprocess.run(
        [
            "docker",
            "exec",
            db_container,
            "psql",
            "-U",
            "orchestra",
            "-d",
            "orchestra",
            "-c",
            f"UPDATE assistants SET is_local = true WHERE agent_id = {int(assistant_id)};",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Failed to mark assistant as local runtime: "
            f"{completed.stderr or completed.stdout}",
        )


def ensure_pubsub_topic_exists(
    urls: LocalStackUrls,
    comms,
    assistant_id: str,
) -> None:
    topic_name = f"unity-{assistant_id}{urls.pubsub_suffix}"
    response = comms.post(
        "/infra/pubsub/topic",
        data={"topic_name": topic_name},
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"infra topic create failed: {response.status_code} {response.text}"


def temporary_inbound_subscription(
    pubsub_subscriber,
    urls: LocalStackUrls,
    assistant_id: str,
    *,
    label: str,
):
    topic_name = f"unity-{assistant_id}{urls.pubsub_suffix}"
    topic_path = pubsub_subscriber.topic_path(urls.gcp_project_id, topic_name)
    subscription_name = f"{topic_name}-{label}-{uuid.uuid4().hex[:12]}"
    subscription_path = pubsub_subscriber.subscription_path(
        urls.gcp_project_id,
        subscription_name,
    )
    pubsub_subscriber.create_subscription(
        request={
            "name": subscription_path,
            "topic": topic_path,
            "filter": 'attributes.thread = "inbound"',
            "expiration_policy": {"ttl": {"seconds": _TEMP_SUBSCRIPTION_TTL_SECONDS}},
            "message_retention_duration": {"seconds": 600},
        },
    )
    return subscription_path


def delete_subscription(pubsub_subscriber, subscription_path: str) -> None:
    try:
        pubsub_subscriber.delete_subscription(
            request={"subscription": subscription_path},
        )
    except NotFound:
        pass


def pull_pubsub_message(
    pubsub_subscriber,
    subscription_path: str,
    *,
    thread: str,
    timeout_seconds: float = 60,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = pubsub_subscriber.pull(
                request={"subscription": subscription_path, "max_messages": 10},
                timeout=10,
            )
        except DeadlineExceeded:
            continue
        if not response.received_messages:
            continue
        ack_ids = [message.ack_id for message in response.received_messages]
        pubsub_subscriber.acknowledge(
            request={"subscription": subscription_path, "ack_ids": ack_ids},
        )
        for received in response.received_messages:
            payload = json.loads(received.message.data.decode("utf-8"))
            if payload.get("thread") == thread:
                return payload
    raise TimeoutError(f"Timed out waiting for Pub/Sub thread={thread!r}")


def post_membership_update(adapters, assistant_id: str) -> None:
    response = adapters.post(
        "/assistant/update",
        data={
            "assistant_id": assistant_id,
            "update_kind": "membership",
        },
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"assistant/update failed: {response.status_code} {response.text}"
