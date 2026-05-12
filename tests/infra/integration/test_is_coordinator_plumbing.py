"""Live integration tests for the Coordinator role wire format."""

import json
import os
import time
import uuid

import pytest
import requests
from google.api_core.exceptions import DeadlineExceeded, NotFound

from communication.infra.assistant_sessions import read_bootstrap_secret

from .conftest import (
    ADMIN_KEY,
    GCP_PROJECT_ID,
    NAMESPACE,
    ORCHESTRA_URL,
    _admin_record_to_data,
    get_assistant_session,
    poll_until,
    stop_assistant_runtime,
)

pytestmark = [pytest.mark.integration]


def _pubsub_suffix() -> str:
    return os.getenv(
        "TEST_PUBSUB_SUFFIX",
        f"-{NAMESPACE}" if NAMESPACE != "production" else "",
    )


def _fetch_admin_assistant(assistant_id: str) -> dict:
    response = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params={"agent_id": assistant_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"admin assistant lookup failed: {response.status_code} {response.text}"
    info = response.json()["info"]
    return info[0] if isinstance(info, list) else info


def _configured_coordinator_assistant() -> dict:
    assistant_id = os.getenv("TEST_COORDINATOR_ASSISTANT_ID", "")
    if not assistant_id:
        pytest.skip(
            "Set TEST_COORDINATOR_ASSISTANT_ID to run Coordinator plumbing tests",
        )
    record = _fetch_admin_assistant(assistant_id)
    assert (
        record.get("is_coordinator") is True
    ), f"TEST_COORDINATOR_ASSISTANT_ID={assistant_id} is not a Coordinator"
    return _admin_record_to_data(record)


def _configured_personal_coordinator_assistant() -> dict:
    assistant_id = os.getenv("TEST_PERSONAL_COORDINATOR_ASSISTANT_ID", "")
    if not assistant_id:
        pytest.skip(
            "Set TEST_PERSONAL_COORDINATOR_ASSISTANT_ID to run personal Coordinator plumbing tests",
        )
    record = _fetch_admin_assistant(assistant_id)
    assert (
        record.get("is_coordinator") is True
    ), f"TEST_PERSONAL_COORDINATOR_ASSISTANT_ID={assistant_id} is not a Coordinator"
    assert not record.get(
        "organization_id",
    ), f"TEST_PERSONAL_COORDINATOR_ASSISTANT_ID={assistant_id} is not personal-scoped"
    return _admin_record_to_data(record)


def _configured_non_coordinator_assistant_id() -> str:
    assistant_id = os.getenv("TEST_NON_COORDINATOR_ASSISTANT_ID", "")
    if not assistant_id:
        pytest.skip(
            "Set TEST_NON_COORDINATOR_ASSISTANT_ID to run non-Coordinator plumbing tests",
        )
    record = _fetch_admin_assistant(assistant_id)
    assert (
        record.get("is_coordinator") is False
    ), f"TEST_NON_COORDINATOR_ASSISTANT_ID={assistant_id} is a Coordinator"
    return str(assistant_id)


def _post_assistant_update(adapters, assistant_id: str) -> None:
    response = adapters.post(
        "/assistant/update",
        data={"assistant_id": assistant_id},
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"assistant/update failed: {response.status_code} {response.text}"


def _read_startup_payload(comms, core_api, assistant_id: str) -> dict:
    session = poll_until(
        lambda: get_assistant_session(comms, assistant_id),
        timeout=180,
        interval=5,
        description=f"AssistantSession for {assistant_id}",
    )
    secret_name = str((session.get("spec") or {}).get("startupSecretRef") or "")
    assert secret_name, f"Expected startupSecretRef on session: {session}"
    return read_bootstrap_secret(core_api, NAMESPACE, secret_name)


def _temporary_inbound_subscription(pubsub_subscriber, assistant_id: str):
    topic_name = f"unity-{assistant_id}{_pubsub_suffix()}"
    topic_path = pubsub_subscriber.topic_path(GCP_PROJECT_ID, topic_name)
    subscription_name = f"{topic_name}-is-coordinator-{uuid.uuid4().hex[:12]}"
    subscription_path = pubsub_subscriber.subscription_path(
        GCP_PROJECT_ID,
        subscription_name,
    )
    pubsub_subscriber.create_subscription(
        request={
            "name": subscription_path,
            "topic": topic_path,
            "filter": 'attributes.thread = "inbound"',
            "expiration_policy": {"ttl": {"seconds": 3600}},
            "message_retention_duration": {"seconds": 600},
        },
    )
    return subscription_path


def _pull_assistant_update_event(pubsub_subscriber, subscription_path: str) -> dict:
    deadline = time.monotonic() + 60
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
            if payload.get("thread") == "assistant_update":
                return payload
    raise TimeoutError("Timed out waiting for assistant_update Pub/Sub event")


def test_coordinator_assistant_carries_true_to_bootstrap_secret(
    adapters,
    comms,
    core_api,
    batch_api,
):
    """A Coordinator wake should write `is_coordinator: true` into bootstrap JSON."""

    assistant = _configured_coordinator_assistant()
    try:
        _post_assistant_update(adapters, assistant["assistant_id"])
        bootstrap_payload = _read_startup_payload(
            comms,
            core_api,
            assistant["assistant_id"],
        )
    finally:
        stop_assistant_runtime(
            assistant["assistant_id"],
            batch_api=batch_api,
            context="is-coordinator-bootstrap",
        )

    assert bootstrap_payload["is_coordinator"] is True


def test_non_coordinator_assistant_carries_false_to_bootstrap_secret(
    adapters,
    comms,
    core_api,
    batch_api,
):
    """A regular assistant wake should write an explicit non-Coordinator bool."""

    assistant_id = _configured_non_coordinator_assistant_id()
    try:
        _post_assistant_update(adapters, assistant_id)
        bootstrap_payload = _read_startup_payload(
            comms,
            core_api,
            assistant_id,
        )
    finally:
        stop_assistant_runtime(
            assistant_id,
            batch_api=batch_api,
            context="is-coordinator-bootstrap-non-coordinator",
        )

    assert bootstrap_payload["is_coordinator"] is False


def test_assistant_update_webhook_publishes_is_coordinator_in_event(
    adapters,
    pubsub_subscriber,
    batch_api,
):
    """The assistant-update event should carry the Coordinator bool in JSON."""

    assistant = _configured_coordinator_assistant()
    subscription_path = _temporary_inbound_subscription(
        pubsub_subscriber,
        assistant["assistant_id"],
    )
    try:
        _post_assistant_update(adapters, assistant["assistant_id"])
        message = _pull_assistant_update_event(pubsub_subscriber, subscription_path)
    finally:
        stop_assistant_runtime(
            assistant["assistant_id"],
            batch_api=batch_api,
            context="is-coordinator-update",
        )
        try:
            pubsub_subscriber.delete_subscription(
                request={"subscription": subscription_path},
            )
        except NotFound:
            pass

    event = message["event"]
    assert event["assistant_id"] == assistant["assistant_id"]
    assert event["is_coordinator"] is True


def test_personal_coordinator_carries_null_org_id_to_bootstrap_and_update_event(
    adapters,
    comms,
    core_api,
    pubsub_subscriber,
    batch_api,
):
    """Personal Coordinators should preserve null org scope across runtime payloads."""

    assistant = _configured_personal_coordinator_assistant()
    subscription_path = _temporary_inbound_subscription(
        pubsub_subscriber,
        assistant["assistant_id"],
    )
    try:
        _post_assistant_update(adapters, assistant["assistant_id"])
        bootstrap_payload = _read_startup_payload(
            comms,
            core_api,
            assistant["assistant_id"],
        )
        message = _pull_assistant_update_event(pubsub_subscriber, subscription_path)
    finally:
        stop_assistant_runtime(
            assistant["assistant_id"],
            batch_api=batch_api,
            context="is-coordinator-personal-scope",
        )
        try:
            pubsub_subscriber.delete_subscription(
                request={"subscription": subscription_path},
            )
        except NotFound:
            pass

    assert bootstrap_payload["is_coordinator"] is True
    assert bootstrap_payload["org_id"] is None

    event = message["event"]
    assert event["assistant_id"] == assistant["assistant_id"]
    assert event["is_coordinator"] is True
    assert event["org_id"] is None
