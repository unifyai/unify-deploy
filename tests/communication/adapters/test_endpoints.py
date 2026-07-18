"""
Integration tests for the FastAPI adapters endpoints.

These tests require a running server and Pub/Sub access.
"""

from dotenv import load_dotenv

load_dotenv()
import os
import base64
from google.cloud import pubsub_v1
import json

import pytest

pytestmark = pytest.mark.live

subscriber = pubsub_v1.SubscriberClient()
subscription_path = subscriber.subscription_path(
    os.getenv("GCP_PROJECT_ID"),
    "unity-default-test-assistant-staging-sub",
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
        subscription=subscription_path,
        max_messages=1,
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
        subscription=subscription_path,
        max_messages=1,
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
        assert data["event"]["livekit_room"] == "unity_default-test-assistant_phone"
        assert data["event"]["sip_uri"].startswith(
            "sip:unity_default-test-assistant_phone@",
        ), f"SIP URI should use room name, got: {data['event']['sip_uri']}"
        assert data["event"]["assistant_id"] == "default-test-assistant"
        assert (
            "call_metadata" in data["event"]
            and data["event"]["call_metadata"] is not None
        )
        assert data["event"]["call_metadata"]["twilio_number"] == assistant_number
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_twilio_sms_webhook(test_client):
    """Test successful SMS webhook processing."""
    endpoint = "/twilio/sms"
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
        subscription=subscription_path,
        max_messages=1,
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
    endpoint = "/twilio/whatsapp"
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
        subscription=subscription_path,
        max_messages=1,
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "whatsapp"
        assert "event" in data and data["event"] is not None
        assert data["event"]["to_number"] == assistant_number
        assert data["event"]["from_number"] == user_number
        assert data["event"]["body"] == body
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_unify_message_webhook(test_client):
    """Test successful unified chat webhook processing (assistant DM)."""
    endpoint = "/unify/chat"
    body = "Hello, this is a unify_message test message"
    json_payload = {
        "kind": "assistant_dm",
        "thread_id": 1,
        "assistant_id": "default-test-assistant",
        "message": {
            "id": 1,
            "thread_id": 1,
            "kind": "assistant_dm",
            "assistant_id": "default-test-assistant",
            "sender_kind": "user",
            "content": body,
        },
        "fanout_assistant_ids": ["default-test-assistant"],
        "assistant_event": {
            "thread_id": 1,
            "thread_kind": "assistant_dm",
            "chat_message_id": 1,
            "body": body,
            "sender_user_id": "",
            "sender_email": "",
            "sender_name": "Test",
        },
    }

    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST",
        endpoint,
        json=json_payload,
        headers=headers,
    )

    assert response.status_code == 200

    # Check that the message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path,
        max_messages=1,
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] in (
            "chat_message",
            "unify_message",
        )
        assert "event" in data and data["event"] is not None
        assert data["event"].get("body") == body or data["event"].get("content") == body
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_log_pre_hire_chats_webhook(test_client):
    """Test successful log_pre_hire_chats webhook processing with body list."""
    endpoint = "/unity/pre-hire"
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
        "POST",
        endpoint,
        json=json_payload,
        headers=headers,
    )

    assert response.status_code == 200

    # Check that the message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path,
        max_messages=1,
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


def test_unify_meet_webhook(test_client):
    """Meet dispatch carries session + roster + opening config to the runtime."""
    endpoint = "/unify/meet"
    room_name = "unity_call_sess-intro-1"
    opening_config = {
        "mode": "simulated",
        "simulated_utterance": "Hello from the coordinator.",
        "source": "coordinator_onboarding_intro",
    }
    participants = [
        {
            "kind": "human",
            "user_id": "user-1",
            "assistant_id": None,
            "display_name": "Ada Owner",
            "contact_id": 1,
            "email": "ada@example.com",
        },
    ]
    json_payload = {
        "room_name": room_name,
        "assistant_id": "default-test-assistant",
        "call_session_id": "sess-intro-1",
        "participants": participants,
        "opening_config": opening_config,
    }

    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST",
        endpoint,
        json=json_payload,
        headers=headers,
    )

    assert response.status_code == 200

    # Check that the message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path,
        max_messages=1,
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "unify_meet"
        assert "event" in data and data["event"] is not None
        assert data["event"]["assistant_id"] == "default-test-assistant"
        assert data["event"]["livekit_room"] == room_name
        assert data["event"]["call_session_id"] == "sess-intro-1"
        assert data["event"]["participants"] == participants
        assert "livekit_agent_name" not in data["event"]
        assert data["event"]["opening_config"] == opening_config
    except AssertionError as e:
        print(e)
        raise
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_unify_meet_webhook_rejects_sessionless_dispatch(test_client):
    """Every meet is a call session with a roster; legacy wakes are rejected."""
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    no_session = test_client.make_request(
        "POST",
        "/unify/meet",
        json={
            "room_name": "unity_default-test-assistant_meet",
            "assistant_id": "default-test-assistant",
        },
        headers=headers,
    )
    assert no_session.status_code == 400

    no_roster = test_client.make_request(
        "POST",
        "/unify/meet",
        json={
            "room_name": "unity_call_sess-2",
            "assistant_id": "default-test-assistant",
            "call_session_id": "sess-2",
        },
        headers=headers,
    )
    assert no_roster.status_code == 400


def test_unify_meet_webhook_org_call_participants(test_client):
    """Org call meet wakes publish participants and omit shared-room agent_name."""
    endpoint = "/unify/meet"
    room_name = "unity_call_abc"
    participants = [
        {
            "kind": "human",
            "user_id": "user-1",
            "assistant_id": None,
            "display_name": "Ada Owner",
            "contact_id": 2,
            "email": "ada@example.com",
        },
        {
            "kind": "assistant",
            "user_id": None,
            "assistant_id": 42,
            "display_name": "Peer Bot",
            "contact_id": 3,
            "email": None,
        },
    ]
    json_payload = {
        "room_name": room_name,
        "assistant_id": "default-test-assistant",
        "call_session_id": "abc",
        "participants": participants,
    }

    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST",
        endpoint,
        json=json_payload,
        headers=headers,
    )

    assert response.status_code == 200

    message = subscriber.pull(
        subscription=subscription_path,
        max_messages=1,
    ).received_messages[0]
    ack_id = message.ack_id
    data = json.loads(message.message.data.decode("utf-8"))
    try:
        assert data["thread"] == "unify_meet"
        event = data["event"]
        assert event["livekit_room"] == room_name
        assert event["call_session_id"] == "abc"
        assert "livekit_agent_name" not in event
        assert event["participants"] == participants
    finally:
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
        "POST",
        endpoint,
        json=form_payload,
        headers=headers,
    )
    assert response.status_code == 200

    # Check that the message was published to Pub/Sub
    pubsub_msg = subscriber.pull(
        subscription=subscription_path,
        max_messages=1,
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
                    },
                ).encode(),
            ).decode(),
        },
    }
    response = test_client.make_request("POST", endpoint, json=data)

    assert response.status_code == 200
    assert response.text == "No new conversations"


def test_infra_maintenance(test_client):
    """Test the unified infra maintenance sweep endpoint."""
    endpoint = "/scheduled/infra/maintenance"
    response = test_client.make_request("POST", endpoint, json={})

    print("Infra maintenance:", response.text)
    assert response.status_code == 200

    data = response.json()
    assert isinstance(data, dict)
    assert isinstance(data["expired"], int)


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
        subscription=subscription_path,
        max_messages=1,
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
        assert event["assistant_first_name"] == "Test"
        assert event["assistant_surname"] == "Assistant"
        assert event["assistant_timezone"] == "UTC"
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])


def test_health_check(test_client):
    """Test health check endpoint returns healthy status."""
    endpoint = "/health"
    response = test_client.make_request("GET", endpoint)

    assert response.status_code == 200
    assert "application/json" in response.headers.get("content-type", "")
    response_data = response.json()
    assert response_data["status"] == "healthy"


def test_assistant_wakeup_webhook(test_client):
    """Test successful assistant wakeup webhook processing."""
    endpoint = "/assistant/wakeup"
    assistant_id = "default-test-assistant"

    data = {"assistant_id": assistant_id}
    response = test_client.make_request("POST", endpoint, data=data)

    print("Assistant wakeup response:", response.text)
    assert response.status_code == 200


def test_microsoft_router_validation_token(test_client):
    """Test Microsoft router returns validation token for subscription setup."""
    endpoint = "/microsoft/router"
    validation_token = "test-validation-token-12345"

    response = test_client.make_request(
        "POST",
        endpoint,
        params={"validationToken": validation_token},
    )

    assert response.status_code == 200
    assert response.text == validation_token


def test_microsoft_auth_callback_missing_code(test_client):
    """Test Microsoft OAuth callback returns error when code is missing."""
    endpoint = "/microsoft/auth/callback"

    response = test_client.make_request("GET", endpoint)

    assert response.status_code == 400
    assert "Missing authorization code" in response.text


def test_microsoft_auth_callback_with_error(test_client):
    """Test Microsoft OAuth callback handles error response from Microsoft."""
    endpoint = "/microsoft/auth/callback"

    response = test_client.make_request(
        "GET",
        endpoint,
        params={
            "error": "access_denied",
            "error_description": "User denied access",
        },
    )

    assert response.status_code == 400
    assert "access_denied" in response.text


def test_microsoft_auth_callback_invalid_state(test_client):
    """Test Microsoft OAuth callback handles invalid state parameter."""
    endpoint = "/microsoft/auth/callback"

    response = test_client.make_request(
        "GET",
        endpoint,
        params={
            "code": "test-code-12345",
            "state": "invalid-base64-state",
        },
    )

    assert response.status_code == 400
    assert "Invalid state parameter" in response.text


def test_scheduled_email_watches(test_client):
    """Test scheduled email watches endpoint with test mode."""
    endpoint = "/scheduled/email-watches"

    response = test_client.make_request("POST", endpoint, json={"test": True})

    print("Email watches response:", response.text)
    assert response.status_code == 200
    response_data = response.json()
    # In test mode, should process only the test assistant
    assert "gmail" in response_data or "outlook" in response_data


def test_scheduled_microsoft_tokens(test_client):
    """Test scheduled Microsoft token refresh endpoint with test mode."""
    endpoint = "/scheduled/microsoft-tokens"

    response = test_client.make_request("POST", endpoint, json={"test": True})

    print("Microsoft tokens response:", response.text)
    assert response.status_code == 200
    response_data = response.json()
    # Should have refreshed and failed lists
    assert "refreshed" in response_data
    assert "failed" in response_data


def test_scheduled_teams_watches(test_client):
    """Test scheduled Teams watches endpoint with test mode."""
    endpoint = "/scheduled/teams-watches"

    response = test_client.make_request("POST", endpoint, json={"test": True})

    print("Teams watches response:", response.text)
    assert response.status_code == 200
    response_data = response.json()
    # Should have chats_renewed, channels_renewed, skipped, and failed lists
    assert "chats_renewed" in response_data or "failed" in response_data


def test_unify_message_webhook_unauthorized(test_client):
    """Test that the unified chat webhook rejects unauthorized requests."""
    endpoint = "/unify/chat"
    json_payload = {
        "kind": "assistant_dm",
        "assistant_id": "default-test-assistant",
        "message": {"content": "Unauthorized test message"},
    }

    # Request without authorization header
    response = test_client.make_request("POST", endpoint, json=json_payload)

    assert response.status_code == 401


def test_unify_message_webhook_missing_assistant_id(test_client):
    """Test that assistant-DM chat frames require assistant_id."""
    endpoint = "/unify/chat"
    json_payload = {
        "kind": "assistant_dm",
        "message": {"content": "Test message without assistant_id"},
    }

    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST",
        endpoint,
        json=json_payload,
        headers=headers,
    )

    assert response.status_code == 400


def test_unify_meet_webhook_unauthorized(test_client):
    """Test that unify_meet webhook rejects unauthorized requests."""
    endpoint = "/unify/meet"
    json_payload = {
        "room_name": "unity_default-test-assistant_meet",
        "assistant_id": "default-test-assistant",
    }

    # Request without authorization header
    response = test_client.make_request("POST", endpoint, json=json_payload)

    assert response.status_code == 401


def test_unify_meet_webhook_missing_required_fields(test_client):
    """Test that unify_meet webhook requires room_name."""
    endpoint = "/unify/meet"
    json_payload = {
        "assistant_id": "default-test-assistant",
        # Missing room_name
    }

    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST",
        endpoint,
        json=json_payload,
        headers=headers,
    )

    assert response.status_code == 400


def test_unity_system_event_webhook_unauthorized(test_client):
    """Test that unity_system_event webhook rejects unauthorized requests."""
    endpoint = "/unity/system-event"
    json_payload = {
        "assistant_id": "default-test-assistant",
        "event_type": "test_event",
        "message": "Unauthorized test message",
    }

    # Request without authorization header
    response = test_client.make_request("POST", endpoint, json=json_payload)

    assert response.status_code == 401


def test_unity_system_event_webhook_missing_fields(test_client):
    """Test that unity_system_event webhook requires all required fields."""
    endpoint = "/unity/system-event"
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}

    # Missing assistant_id
    response = test_client.make_request(
        "POST",
        endpoint,
        json={"event_type": "test", "message": "test"},
        headers=headers,
    )
    assert response.status_code == 400

    # Missing event_type
    response = test_client.make_request(
        "POST",
        endpoint,
        json={"assistant_id": "test", "message": "test"},
        headers=headers,
    )
    assert response.status_code == 400

    # Missing message
    response = test_client.make_request(
        "POST",
        endpoint,
        json={"assistant_id": "test", "event_type": "test"},
        headers=headers,
    )
    assert response.status_code == 400


def test_unity_pre_hire_webhook_unauthorized(test_client):
    """Test that unity_pre_hire webhook rejects unauthorized requests."""
    endpoint = "/unity/pre-hire"
    json_payload = {
        "assistant_id": "default-test-assistant",
        "body": [{"role": "user", "msg": "test"}],
    }

    # Request without authorization header
    response = test_client.make_request("POST", endpoint, json=json_payload)

    assert response.status_code == 401


def test_unity_pre_hire_webhook_invalid_body_format(test_client):
    """Test that unity_pre_hire webhook validates body format."""
    endpoint = "/unity/pre-hire"
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}

    # Body is not a list
    response = test_client.make_request(
        "POST",
        endpoint,
        json={"assistant_id": "default-test-assistant", "body": "invalid"},
        headers=headers,
    )
    assert response.status_code == 400

    # Body items missing required fields
    response = test_client.make_request(
        "POST",
        endpoint,
        json={"assistant_id": "default-test-assistant", "body": [{"role": "user"}]},
        headers=headers,
    )
    assert response.status_code == 400


def test_outlook_notification_missing_client_state(test_client):
    """Test Outlook notification processor with missing client state email."""
    endpoint = "/email/outlook"
    # Legacy format clientState without email encoded
    notification = {
        "clientState": "unify-outlook-webhook",  # No email encoded
        "resource": "/users/test/mailFolders/inbox/messages/123",
    }

    response = test_client.make_request("POST", endpoint, json=notification)

    # Should return 200 but not process (legacy format)
    assert response.status_code == 200


def test_outlook_notification_invalid_secret(test_client):
    """Test Outlook notification processor rejects invalid secret."""
    endpoint = "/email/outlook"
    notification = {
        "clientState": "wrong-secret::test@test.com",
        "resource": "/users/test/mailFolders/inbox/messages/123",
    }

    response = test_client.make_request("POST", endpoint, json=notification)

    # Should return 200 but not process
    assert response.status_code == 200


def test_microsoft_router_routes_outlook_notification(test_client):
    """Test Microsoft router handles routing notifications."""
    endpoint = "/microsoft/router"
    notification_payload = {
        "value": [
            {
                "resource": "/users/test/mailFolders/inbox/Messages/123",
                "clientState": "unify-outlook-webhook::test@test.com",
            },
        ],
    }

    response = test_client.make_request("POST", endpoint, json=notification_payload)

    print("Microsoft router response:", response.text)
    assert response.status_code == 200
    assert response.text == "OK"


def test_microsoft_router_routes_teams_notification(test_client):
    """Test Microsoft router routes Teams chat notifications correctly."""
    endpoint = "/microsoft/router"
    notification_payload = {
        "value": [
            {
                "resource": "/chats/19:meeting_abc@thread.v2/messages/123",
                "clientState": "unify-teams-webhook::test@test.com",
            },
        ],
    }

    response = test_client.make_request("POST", endpoint, json=notification_payload)

    print("Microsoft router teams response:", response.text)
    assert response.status_code == 200
    assert response.text == "OK"


def test_microsoft_router_routes_teams_notification_odata_form(test_client):
    """Graph delivers Teams chat notifications in OData form for
    /me/chats/getAllMessages subscriptions (e.g. group/federated chats with
    prefixes like `19:uni01_...@thread.v2`). The router must recognize this
    form and forward it instead of dropping it as unknown."""
    endpoint = "/microsoft/router"
    notification_payload = {
        "value": [
            {
                "resource": (
                    "chats('19:uni01_abc@thread.v2')/messages('1776409346815')"
                ),
                "clientState": "unify-teams-webhook::test@test.com",
            },
        ],
    }

    response = test_client.make_request("POST", endpoint, json=notification_payload)

    print("Microsoft router teams (odata) response:", response.text)
    assert response.status_code == 200
    assert response.text == "OK"


# =============================================================================
# LiveKit Recording Webhook Tests
# =============================================================================


def _sign_livekit_webhook(body: str) -> str:
    """Generate a valid LiveKit webhook Authorization token for a given body.

    Uses the same signing mechanism that LiveKit Egress uses: SHA256 of the
    body placed in a JWT claim, signed with LIVEKIT_API_SECRET.
    """
    import hashlib
    from livekit.api import AccessToken

    body_hash = hashlib.sha256(body.encode()).digest()
    sha256_b64 = base64.b64encode(body_hash).decode()

    token = (
        AccessToken(
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
        )
        .with_sha256(sha256_b64)
        .to_jwt()
    )
    return token


def test_livekit_recording_webhook_rejects_invalid_signature(test_client):
    """Test that the recording webhook rejects requests with bad signatures."""
    endpoint = "/livekit/recording-complete"
    response = test_client.make_request(
        "POST",
        endpoint,
        data="invalid-body",
        headers={"Authorization": "Bearer bad-token"},
    )

    assert response.status_code == 401


def test_livekit_recording_webhook_happy_path(test_client):
    """Test successful recording webhook processing.

    Sends a properly signed LiveKit egress_ended event and verifies that a
    recording_ready Pub/Sub message is published with the correct fields.
    """
    endpoint = "/livekit/recording-complete"
    assistant_id = "default-test-assistant"
    room_name = "unity_test_room"

    egress_body = json.dumps(
        {
            "event": "egress_ended",
            "egressInfo": {
                "egressId": "eg-test-123",
                "roomName": room_name,
                "status": 0,
                "fileResults": [
                    {
                        "filename": f"staging/{assistant_id}/{room_name}.mp3",
                        "size": 123456,
                    },
                ],
            },
        },
    )
    auth_token = _sign_livekit_webhook(egress_body)

    user_id = "default-test-user"
    response = test_client.make_request(
        "POST",
        f"{endpoint}?assistant_id={assistant_id}&user_id={user_id}&room_name={room_name}",
        data=egress_body,
        headers={
            "Authorization": auth_token,
            "Content-Type": "application/json",
        },
    )

    print("Recording webhook response:", response.text)
    assert response.status_code == 200
    assert response.json()["success"] is True

    # Check that the recording_ready message was published to Pub/Sub
    message = subscriber.pull(
        subscription=subscription_path,
        max_messages=1,
    ).received_messages[0]
    ack_id = message.ack_id
    message = message.message
    try:
        data = json.loads(message.data.decode("utf-8"))
    except json.JSONDecodeError:
        assert False, "Failed to decode message data"
    try:
        assert data is not None
        assert "thread" in data and data["thread"] == "recording_ready"
        assert "event" in data and data["event"] is not None
        assert data["event"]["assistant_id"] == assistant_id
        assert data["event"]["user_id"] == user_id
        assert data["event"]["conference_name"] == room_name
        assert "storage.googleapis.com" in data["event"]["recording_url"]
        assert f"{room_name}.mp3" in data["event"]["recording_url"]
    except AssertionError as e:
        print(e)
    subscriber.acknowledge(subscription=subscription_path, ack_ids=[ack_id])
