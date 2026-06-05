"""Live integration tests for the Coordinator role wire format."""

import json
import os
import time
import uuid

import pytest
import requests
from google.api_core.exceptions import DeadlineExceeded, NotFound
from kubernetes.client.exceptions import ApiException

from communication.infra.assistant_sessions import read_bootstrap_secret

from .conftest import (
    ADMIN_KEY,
    COMMS_APP_URL,
    GCP_PROJECT_ID,
    NAMESPACE,
    ORCHESTRA_URL,
    _admin_record_to_data,
    get_assistant_session,
    poll_until,
    stop_assistant_runtime,
)

pytestmark = [pytest.mark.integration]

_TEMP_SUBSCRIPTION_TTL_SECONDS = 86_400


def _pubsub_suffix() -> str:
    return os.getenv(
        "TEST_PUBSUB_SUFFIX",
        f"-{NAMESPACE}" if NAMESPACE != "production" else "",
    )


def _coordinator_provisioning_url() -> str:
    return os.getenv("TEST_COORDINATOR_ORCHESTRA_URL", ORCHESTRA_URL).rstrip("/")


def _fetch_user_api_key(user_id: str) -> str:
    response = requests.get(
        f"{ORCHESTRA_URL}/admin/user/by-user-id",
        params={"user_id": user_id},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"admin user lookup failed: {response.status_code} {response.text}"
    api_key = response.json().get("api_key", "")
    assert api_key, f"admin user lookup missing api_key for user_id={user_id}"
    return str(api_key)


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


def _create_temp_user_for_personal_coordinator() -> str:
    token = uuid.uuid4().hex[:12]
    response = requests.post(
        f"{ORCHESTRA_URL}/admin/user",
        json={
            "email": f"coordinator-plumbing-{token}@unify.ai",
            "name": "Coordinator",
            "last_name": "Plumbing",
            "timezone": "UTC",
        },
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"admin user create failed: {response.status_code} {response.text}"
    user_id = response.json().get("id")
    assert user_id, f"admin user create missing id: {response.text}"
    return str(user_id)


def _delete_temp_user_for_personal_coordinator(user_id: str) -> None:
    response = requests.delete(
        f"{ORCHESTRA_URL}/admin/user",
        params={"user_id": user_id, "force": "true"},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=90,
    )
    # Teardown should be best-effort to avoid masking contract assertions.
    if response.status_code not in {200, 404}:
        print(
            f"[Teardown] Failed to delete temporary personal-coordinator user {user_id}: "
            f"{response.status_code} {response.text}",
        )


def _wait_for_personal_coordinator_assistant_id(
    user_id: str,
    timeout: int = 120,
) -> str:
    def _snapshot():
        response = requests.get(
            f"{ORCHESTRA_URL}/admin/assistant/user/{user_id}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
        return {
            "status_code": response.status_code,
            "body": response.json() if response.content else response.text,
        }

    def _lookup():
        response = requests.get(
            f"{ORCHESTRA_URL}/admin/assistant/user/{user_id}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
        if response.status_code != 200:
            return None
        info = response.json().get("info", [])
        assistant_rows = info if isinstance(info, list) else [info]
        for row in assistant_rows:
            if row.get("is_coordinator") is not True or row.get("organization_id"):
                continue
            if row.get("self_contact_id") is None or row.get("boss_contact_id") is None:
                continue
            return str(row["agent_id"])
        return None

    assistant_id = poll_until(
        _lookup,
        timeout=timeout,
        interval=5,
        description=f"personal Coordinator assistant for temporary user {user_id}",
        failure_snapshot=_snapshot,
    )
    return assistant_id


def _trigger_personal_coordinator_provision(user_id: str) -> None:
    provisioning_url = _coordinator_provisioning_url()
    user_api_key = _fetch_user_api_key(user_id)
    response = requests.post(
        f"{provisioning_url}/user/{user_id}/coordinator",
        headers={"Authorization": f"Bearer {user_api_key}"},
        timeout=30,
    )
    if response.status_code in {200, 201}:
        return
    if response.status_code == 404:
        raise RuntimeError(
            "Personal Coordinator provisioning endpoint is unavailable at "
            f"{provisioning_url}. Configure TEST_COORDINATOR_ORCHESTRA_URL to a "
            "Coordinator-aware Orchestra host to run this contract check.",
        )
    if response.status_code >= 500:
        # Some preview revisions have returned 500 while still materializing
        # the coordinator row. Continue and verify via admin readback.
        print(
            "[Setup] Personal Coordinator provision request returned "
            f"{response.status_code}: {response.text}",
        )
        return
    raise AssertionError(
        "Personal Coordinator provision request failed: "
        f"{response.status_code} {response.text}",
    )


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


def _ensure_pubsub_topic_exists(assistant_id: str) -> None:
    topic_name = f"unity-{assistant_id}{_pubsub_suffix()}"
    response = requests.post(
        f"{COMMS_APP_URL}/infra/pubsub/topic",
        data={"topic_name": topic_name},
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        timeout=30,
    )
    assert (
        response.status_code == 200
    ), f"infra topic create failed: {response.status_code} {response.text}"


def _read_startup_payload(comms, core_api, assistant_id: str) -> dict:
    def _read_running_session_payload():
        session = get_assistant_session(comms, assistant_id)
        if session is None:
            return None
        spec = session.get("spec") or {}
        if str(spec.get("desiredState", "") or "") != "Running":
            return None
        secret_name = str(spec.get("startupSecretRef") or "")
        if not secret_name:
            return None
        try:
            payload = read_bootstrap_secret(core_api, NAMESPACE, secret_name)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return session, payload

    _session, payload = poll_until(
        _read_running_session_payload,
        timeout=180,
        interval=5,
        description=(
            f"AssistantSession for {assistant_id} to reach Running with readable startup secret"
        ),
        failure_snapshot=lambda: get_assistant_session(comms, assistant_id),
    )
    return payload


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
            "expiration_policy": {"ttl": {"seconds": _TEMP_SUBSCRIPTION_TTL_SECONDS}},
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

    user_id = _create_temp_user_for_personal_coordinator()
    assistant_id: str | None = None
    subscription_path: str | None = None
    try:
        try:
            assistant_id = _wait_for_personal_coordinator_assistant_id(
                user_id,
                timeout=10,
            )
        except TimeoutError:
            try:
                _trigger_personal_coordinator_provision(user_id)
            except RuntimeError as exc:
                pytest.skip(str(exc))
            try:
                assistant_id = _wait_for_personal_coordinator_assistant_id(
                    user_id,
                    timeout=60,
                )
            except TimeoutError as exc:
                pytest.skip(
                    "Unable to provision a fully initialized personal coordinator "
                    f"for integration validation: {exc}",
                )
        _ensure_pubsub_topic_exists(assistant_id)
        subscription_path = _temporary_inbound_subscription(
            pubsub_subscriber,
            assistant_id,
        )
        _post_assistant_update(adapters, assistant_id)
        bootstrap_payload = _read_startup_payload(
            comms,
            core_api,
            assistant_id,
        )
        message = _pull_assistant_update_event(pubsub_subscriber, subscription_path)
    finally:
        if assistant_id is not None:
            stop_assistant_runtime(
                assistant_id,
                batch_api=batch_api,
                context="is-coordinator-personal-scope",
            )
        try:
            if subscription_path is not None:
                pubsub_subscriber.delete_subscription(
                    request={"subscription": subscription_path},
                )
        except NotFound:
            pass
        _delete_temp_user_for_personal_coordinator(user_id)

    assert bootstrap_payload["is_coordinator"] is True
    assert bootstrap_payload["org_id"] is None

    event = message["event"]
    assert event["assistant_id"] == assistant_id
    assert event["is_coordinator"] is True
    assert event["org_id"] is None
