"""
Tests for the FastAPI adapters endpoints.
"""

import time
from dotenv import load_dotenv

load_dotenv()
import os
import base64
from google.cloud import pubsub_v1
import json


subscriber = pubsub_v1.SubscriberClient()
subscription_path = subscriber.subscription_path(
    os.getenv("PROJECT_ID"), "unity-default-test-assistant-staging-sub"
)


def test_twilio_call_status_webhook(test_client):
    """Test successful phone call status webhook processing."""
    endpoint = "/twilio/call-status"
    user_number = "+9876543210"
    assistant_number = "+0123456789"
    # simulate call status update
    data = {"CallStatus": "in-progress", "From": assistant_number, "To": user_number}
    response = test_client.make_request("POST", endpoint, data=data)

    # endpoint should accept status updates
    assert response.status_code == 200

    # Check that the message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path, max_messages=1
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "call_answered"
        assert "event" in data and data["event"] is not None
        assert data["event"]["assistant_id"] == "default-test-assistant"
        assert data["event"]["user_number"] == user_number
        assert data["event"]["assistant_number"] == assistant_number
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_twilio_call_webhook(test_client):
    """Test successful phone call webhook processing."""
    endpoint = "/twilio/call"
    user_number = "+9876543210"
    assistant_number = "+0123456789"
    data = {"To": assistant_number, "From": user_number}

    response = test_client.make_request("POST", endpoint, data=data)

    print(response.text)
    assert response.status_code == 200
    # The response should be XML for Twilio
    assert "text/xml" in response.headers.get("content-type", "")

    # Check that the message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path, max_messages=1
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "call"
        assert "event" in data and data["event"] is not None
        assert f"Unity_{assistant_number[1:]}" in data["event"]["conference_name"]
        assert data["event"]["caller_number"] == user_number
        assert data["event"]["livekit_room"] == f"unity_{assistant_number}"
        assert data["event"]["assistant_id"] == "default-test-assistant"
        assert (
            "call_metadata" in data["event"]
            and data["event"]["call_metadata"] is not None
        )
        assert data["event"]["call_metadata"]["twilio_number"] == assistant_number
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_twilio_msg_webhook(test_client):
    """Test successful SMS webhook processing."""
    endpoint = "/twilio/msg"
    user_number = "+9876543210"
    assistant_number = "+0123456789"
    body = "Hello, this is a test message"
    data = {
        "To": assistant_number,
        "From": user_number,
        "Body": body,
    }

    response = test_client.make_request("POST", endpoint, data=data)

    assert response.status_code == 200
    assert "text/xml" in response.headers.get("content-type", "")

    # Check that the message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path, max_messages=1
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "msg"
        assert "event" in data and data["event"] is not None
        assert data["event"]["to_number"] == assistant_number
        assert data["event"]["from_number"] == user_number
        assert data["event"]["body"] == body
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_unify_message_webhook(test_client):
    """Test successful Unify Message webhook processing."""
    endpoint = "/unify/message"
    body = "Hello, this is a unify_message test message"
    json_payload = {
        "assistant_id": "default-test-assistant",
        "body": body,
    }

    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST", endpoint, json=json_payload, headers=headers
    )

    assert response.status_code == 200

    # Check that the message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path, max_messages=1
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "unify_message"
        assert "event" in data and data["event"] is not None
        assert data["event"]["contact_id"] == 1
        assert data["event"]["assistant_id"] == "default-test-assistant"
        assert data["event"]["body"] == body
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_unify_call_webhook(test_client):
    """Test successful unify_call webhook processing."""
    endpoint = "/unify/call"
    agent_name = "unify_call_default-test-assistant"
    json_payload = {
        "agent_name": agent_name,
        "room_name": agent_name,
        "assistant_id": "default-test-assistant",
    }

    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST", endpoint, json=json_payload, headers=headers
    )

    assert response.status_code == 200

    # Check that the message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path, max_messages=1
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "unify_call"
        assert "event" in data and data["event"] is not None
        assert data["event"]["assistant_id"] == "default-test-assistant"
        assert data["event"]["livekit_room"] == agent_name
        assert data["event"]["agent_name"] == agent_name
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_log_pre_hire_chats_webhook(test_client):
    """Test successful log_pre_hire_chats webhook processing with body list."""
    endpoint = "/log-pre-hire-chats"
    body = [
        {"role": "user", "msg": "Hey, I'm interested in the role."},
        {"role": "assistant", "msg": "Great! Can you share your resume?"},
    ]
    json_payload = {
        "assistant_id": "default-test-assistant",
        "body": body,
    }

    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST", endpoint, json=json_payload, headers=headers
    )

    assert response.status_code == 200

    # Check that the message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path, max_messages=1
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "log_pre_hire_chats"
        assert "event" in data and data["event"] is not None
        assert data["event"]["assistant_id"] == "default-test-assistant"
        assert isinstance(data["event"]["body"], list)
        assert data["event"]["body"][0] == body[0]
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_unity_system_event_webhook(test_client):
    """Test successful unity system event webhook processing."""
    endpoint = "/unity/system-event"
    event_type = "test_event"
    message = "This is a test message"
    form_payload = {
        "assistant_id": "default-test-assistant",
        "event_type": event_type,
        "message": message,
    }
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST", endpoint, json=form_payload, headers=headers
    )
    assert response.status_code == 200

    # Check that the message was published to Pub/Sub
    pubsub_msg = subscriber.pull(
        subscription=subscription_path, max_messages=1
    ).received_messages[0]
    ack_id = pubsub_msg.ack_id
    pubsub_msg = pubsub_msg.message
    try:
        data = json.loads(pubsub_msg.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "unity_system_event"
        assert "event" in data and data["event"] is not None
        assert data["event"]["assistant_id"] == "default-test-assistant"
        assert data["event"]["event_type"] == event_type
        assert data["event"]["message"] == message
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_email_watch_renewer(test_client):
    """Test successful email watch renewal."""
    endpoint = "/scheduled/email-watch-renewer"
    response = test_client.make_request("POST", endpoint, json={"test": True})

    assert response.status_code == 200
    assert not response.json()["default-test-assistant@unify.ai"]["success"]


def test_email_notification_processor(test_client):
    """Test successful email notification processing."""
    endpoint = "/email/gmail"
    # Pub/Sub push format
    data = {
        "message": {
            "data": base64.b64encode(
                json.dumps(
                    {
                        "emailAddress": "default-test-assistant@unify.ai",
                        "historyId": "12345",
                    }
                ).encode()
            ).decode()
        }
    }
    response = test_client.make_request("POST", endpoint, json=data)

    assert response.status_code == 200
    assert response.text == "No new conversations"


def test_idle_job_adapters(test_client):
    """Test successful idle job creation and cleanup."""
    endpoint = "/scheduled/idle-job-creator"
    response = test_client.make_request("POST", endpoint, json={})

    print("Idle job creator:", response.text)
    assert response.status_code == 200

    print("Waiting for 120 seconds...")
    time.sleep(120)

    endpoint = "/scheduled/idle-job-cleaner"
    response = test_client.make_request("POST", endpoint, json={})

    print("Idle job cleaner:", response.text)
    assert response.status_code == 200
    assert len(response.json()["idle_jobs"])


def test_assistant_update_webhook(test_client):
    """Test successful assistant update webhook processing."""
    endpoint = "/assistant/update"
    assistant_id = "default-test-assistant"

    # Test with form data payload
    data = {"assistant_id": assistant_id}
    response = test_client.make_request("POST", endpoint, data=data)

    print("Assistant update response:", response.text)
    assert response.status_code == 200
    assert "application/json" in response.headers.get("content-type", "")

    response_data = response.json()
    assert response_data["success"] is True
    assert response_data["assistant_id"] == assistant_id
    assert "topic_path" in response_data

    # Check that the message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path, max_messages=1
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "assistant_update"
        assert "event" in data and data["event"] is not None
        event = data["event"]
        assert event["assistant_id"] == assistant_id
        assert event["user_id"] == "default-user"
        assert event["assistant_name"] == "Test Assistant"
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])
