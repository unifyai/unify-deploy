"""
Tests for the Flask wrapper endpoints for the adapters.
"""

import time
from dotenv import load_dotenv

load_dotenv()
import os
from google.cloud import pubsub_v1
import json


subscriber = pubsub_v1.SubscriberClient()
subscription_path = subscriber.subscription_path(
    os.getenv("PROJECT_ID"), "unity-default-test-assistant-staging-sub"
)


def test_twilio_call_status_webhook(test_client):
    """Test successful phone call status webhook processing."""
    endpoint = "/call-status"
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
        assert "thread" in data and data["thread"] == "call_received"
        assert "event" in data and data["event"] is not None
        assert data["event"]["assistant_id"] == "default-test-assistant"
        assert data["event"]["user_number"] == user_number
        assert data["event"]["assistant_number"] == assistant_number
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_twilio_call_webhook(test_client):
    """Test successful phone call webhook processing."""
    endpoint = "/call"
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
    endpoint = "/msg"
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


def test_twilio_whatsapp_webhook(test_client):
    """Test successful WhatsApp webhook processing."""
    endpoint = "/whatsapp"
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


def test_email_watch_renewer(test_client):
    """Test successful email watch renewal."""
    endpoint = "/email/watch"
    response = test_client.make_request("POST", endpoint, json={"test": True})

    assert response.status_code == 200
    assert not response.json()["default-test-assistant@unify.ai"]["success"]


def test_email_notification_processor(test_client):
    """Test successful email notification processing."""
    endpoint = "/email"
    data = {
        "message": {
            "emailAddress": "default-test-assistant@unify.ai",
            "historyId": "12345",
        }
    }
    response = test_client.make_request("POST", endpoint, json=data)

    assert response.status_code == 200
    assert response.text == "No new conversations"


def test_idle_job_adapters(test_client):
    """Test successful idle job creation and cleanup."""
    endpoint = "/job/create"
    response = test_client.make_request("POST", endpoint)

    print("Idle job creator:", response.text)
    assert response.status_code == 200

    print("Waiting for 60 seconds...")
    time.sleep(60)

    endpoint = "/job/clean"
    response = test_client.make_request("POST", endpoint)

    print("Idle job cleaner:", response.text)
    assert response.status_code == 200
    assert len(response.json()["idle_jobs"])
