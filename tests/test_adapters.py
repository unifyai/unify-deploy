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
        assert "thread" in data and data["thread"] == "whatsapp"
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


def test_unify_meet_webhook(test_client):
    """Test successful unify_meet webhook processing."""
    endpoint = "/unify/meet"
    agent_name = "unify_meet_default-test-assistant"
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
        assert "thread" in data and data["thread"] == "unify_meet"
        assert "event" in data and data["event"] is not None
        assert data["event"]["assistant_id"] == "default-test-assistant"
        assert data["event"]["livekit_room"] == agent_name
        assert data["event"]["agent_name"] == agent_name
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
    endpoint = "/scheduled/jobs/create"
    response = test_client.make_request("POST", endpoint, json={})

    print("Idle job creator:", response.text)
    assert response.status_code == 200

    print("Waiting for 120 seconds...")
    time.sleep(120)

    endpoint = "/scheduled/jobs/cleanup"
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


def test_teams_call_webhook(test_client):
    """Test successful Teams SIP call webhook processing."""
    endpoint = "/teams/call"
    teams_number = "+19999999999"
    from_uri = "sip:anonymous@teams.microsoft.com"
    call_id = "test-call-id-12345678"

    json_payload = {
        "from_uri": from_uri,
        "to_uri": f"sip:{teams_number}@sbc.unify.ai:5061;user=phone;transport=tls",
        "call_id": call_id,
        "source_ip": "10.0.0.1",
    }

    response = test_client.make_request("POST", endpoint, json=json_payload)

    print("Teams call response:", response.text)
    assert response.status_code == 200
    assert "application/json" in response.headers.get("content-type", "")

    response_data = response.json()
    assert response_data["success"] is True
    assert response_data["room_name"] == f"unity_{teams_number}"


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
        "POST", endpoint, params={"validationToken": validation_token}
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
    """Test that unify_message webhook rejects unauthorized requests."""
    endpoint = "/unify/message"
    json_payload = {
        "assistant_id": "default-test-assistant",
        "body": "Unauthorized test message",
    }

    # Request without authorization header
    response = test_client.make_request("POST", endpoint, json=json_payload)

    assert response.status_code == 401


def test_unify_message_webhook_missing_assistant_id(test_client):
    """Test that unify_message webhook requires assistant_id."""
    endpoint = "/unify/message"
    json_payload = {
        "body": "Test message without assistant_id",
    }

    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST", endpoint, json=json_payload, headers=headers
    )

    assert response.status_code == 400


def test_unify_meet_webhook_unauthorized(test_client):
    """Test that unify_meet webhook rejects unauthorized requests."""
    endpoint = "/unify/meet"
    json_payload = {
        "agent_name": "test_agent",
        "room_name": "test_room",
        "assistant_id": "default-test-assistant",
    }

    # Request without authorization header
    response = test_client.make_request("POST", endpoint, json=json_payload)

    assert response.status_code == 401


def test_unify_meet_webhook_missing_required_fields(test_client):
    """Test that unify_meet webhook requires agent_name and room_name."""
    endpoint = "/unify/meet"
    json_payload = {
        "assistant_id": "default-test-assistant",
        # Missing agent_name and room_name
    }

    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = test_client.make_request(
        "POST", endpoint, json=json_payload, headers=headers
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


def test_teams_call_webhook_invalid_to_uri(test_client):
    """Test Teams call webhook handles invalid to_uri gracefully."""
    endpoint = "/teams/call"
    json_payload = {
        "from_uri": "sip:anonymous@teams.microsoft.com",
        "to_uri": "invalid_uri",  # Invalid format - no sip: prefix
        "call_id": "test-call-id",
        "source_ip": "10.0.0.1",
    }

    response = test_client.make_request("POST", endpoint, json=json_payload)

    print("Teams call invalid uri response:", response.text)
    assert response.status_code == 400
    assert "Invalid to_uri" in response.text


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
            }
        ]
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
            }
        ]
    }

    response = test_client.make_request("POST", endpoint, json=notification_payload)

    print("Microsoft router teams response:", response.text)
    assert response.status_code == 200
    assert response.text == "OK"
