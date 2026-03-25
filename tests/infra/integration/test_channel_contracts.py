"""
Behavioral contract tests for message-channel adapter endpoints.

Each test verifies that an endpoint:
1. Accepts a well-formed request
2. Returns the expected response shape
3. Produces the correct Pub/Sub side effect (where verifiable)

These are integration tests that hit the real deployed preview services
with real credentials. No mocks or stubs — every assertion exercises
production code paths end-to-end.

Endpoints covered:
- POST /twilio/sms (Twilio signature required)
- POST /twilio/whatsapp (Twilio signature required)
- POST /twilio/call-status (Twilio signature required)
- POST /assistant/update (admin key)
- POST /unity/pre-hire (admin key)
- POST /api/message (admin key)
"""

import json
import time
import uuid

import pytest
import requests

from .conftest import (
    ADAPTERS_URL,
    ADMIN_KEY,
    compute_twilio_signature,
    find_assistant_with_phone,
)

pytestmark = [pytest.mark.staging]


# ---------------------------------------------------------------------------
# Twilio channel tests (require signature + assistant with phone)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def phone_assistant():
    """Find a real assistant with a Twilio phone number."""
    assistant = find_assistant_with_phone()
    if not assistant:
        pytest.skip(
            "No assistant with a phone number found — cannot test Twilio webhooks",
        )
    return assistant


def _twilio_post(path, params, auth_token):
    """POST to a Twilio-validated adapter endpoint with a computed signature."""
    url = f"{ADAPTERS_URL}{path}"
    signature = compute_twilio_signature(url, params, auth_token)
    return requests.post(
        url,
        data=params,
        headers={
            "X-Twilio-Signature": signature,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": ADAPTERS_URL.replace("https://", ""),
        },
        timeout=30,
    )


class TestTwilioSMS:
    """Contract: POST /twilio/sms accepts a signed request with To/From/Body,
    publishes to Pub/Sub, and returns TwiML XML."""

    def test_sms_returns_twiml(self, twilio_auth_token, phone_assistant):
        params = {
            "To": phone_assistant["phone"],
            "From": "+15005550006",
            "Body": f"Contract test SMS {int(time.time())}",
            "MessageSid": f"SM{uuid.uuid4().hex}",
        }
        resp = _twilio_post("/twilio/sms", params, twilio_auth_token)
        assert (
            resp.status_code == 200
        ), f"SMS webhook failed: {resp.status_code} {resp.text}"
        assert "text/xml" in resp.headers.get("content-type", "")

    def test_sms_invalid_signature_rejected(self, phone_assistant):
        params = {
            "To": phone_assistant["phone"],
            "From": "+15005550006",
            "Body": "bad sig test",
        }
        resp = requests.post(
            f"{ADAPTERS_URL}/twilio/sms",
            data=params,
            headers={"X-Twilio-Signature": "invalid"},
            timeout=30,
        )
        assert resp.status_code in (
            403,
            401,
        ), f"Expected 401/403 for bad signature, got {resp.status_code}"


class TestTwilioWhatsApp:
    """Contract: POST /twilio/whatsapp accepts a signed request,
    publishes to Pub/Sub, and returns TwiML XML."""

    def test_whatsapp_returns_twiml(self, twilio_auth_token):
        from .conftest import _fetch_all_user_assistants

        wa_assistant = None
        for a in _fetch_all_user_assistants():
            if a.get("assistant_whatsapp_number"):
                wa_assistant = a
                break
        if not wa_assistant:
            pytest.skip(
                "No assistant with a WhatsApp number — "
                "WhatsApp lookup requires assistant_whatsapp_number in Orchestra",
            )
        wa_number = wa_assistant["assistant_whatsapp_number"]
        params = {
            "To": f"whatsapp:{wa_number}",
            "From": "whatsapp:+15005550006",
            "Body": f"Contract test WhatsApp {int(time.time())}",
            "MessageSid": f"SM{uuid.uuid4().hex}",
        }
        resp = _twilio_post("/twilio/whatsapp", params, twilio_auth_token)
        assert (
            resp.status_code == 200
        ), f"WhatsApp webhook failed: {resp.status_code} {resp.text}"
        assert "text/xml" in resp.headers.get("content-type", "")


class TestTwilioCallStatus:
    """Contract: POST /twilio/call-status accepts a signed status callback
    and returns 200."""

    def test_call_status_completed(self, twilio_auth_token, phone_assistant):
        params = {
            "CallStatus": "completed",
            "From": phone_assistant["phone"],
            "To": "+15005550006",
            "CallSid": f"CA{uuid.uuid4().hex}",
            "Duration": "30",
        }
        resp = _twilio_post("/twilio/call-status", params, twilio_auth_token)
        assert (
            resp.status_code == 200
        ), f"Call status webhook failed: {resp.status_code} {resp.text}"


# ---------------------------------------------------------------------------
# Admin-key authenticated adapter endpoints
# ---------------------------------------------------------------------------


class TestAssistantUpdate:
    """Contract: POST /assistant/update publishes an assistant_update event
    to the assistant's Pub/Sub topic and returns JSON with success=true."""

    def test_update_publishes_event(self, real_assistant_data):
        assistant_id = str(real_assistant_data["assistant_id"])
        resp = requests.post(
            f"{ADAPTERS_URL}/assistant/update",
            data={"assistant_id": assistant_id},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
        assert (
            resp.status_code == 200
        ), f"assistant/update failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert body["success"] is True
        assert body["assistant_id"] == assistant_id
        assert "topic_path" in body


class TestPreHireChat:
    """Contract: POST /unity/pre-hire publishes a log_pre_hire_chats event
    to Pub/Sub and returns 200."""

    def test_pre_hire_publishes_event(self, real_assistant_data):
        assistant_id = str(real_assistant_data["assistant_id"])
        chat_body = json.dumps(
            [
                {"role": "user", "msg": "Contract test pre-hire message"},
                {"role": "assistant", "msg": "Contract test pre-hire response"},
            ],
        )
        resp = requests.post(
            f"{ADAPTERS_URL}/unity/pre-hire",
            data={"assistant_id": assistant_id, "body": chat_body},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
        assert (
            resp.status_code == 200
        ), f"unity/pre-hire failed: {resp.status_code} {resp.text}"


class TestApiMessage:
    """Contract: POST /api/message publishes an api_message event to Pub/Sub
    and returns 200."""

    def test_api_message_publishes_event(self, real_assistant_data):
        assistant_id = str(real_assistant_data["assistant_id"])
        resp = requests.post(
            f"{ADAPTERS_URL}/api/message",
            json={
                "assistant_id": assistant_id,
                "api_message_id": f"contract-test-{uuid.uuid4().hex[:8]}",
                "body": "Contract test API message",
            },
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
        assert (
            resp.status_code == 200
        ), f"api/message failed: {resp.status_code} {resp.text}"

    def test_api_message_rejects_missing_fields(self):
        resp = requests.post(
            f"{ADAPTERS_URL}/api/message",
            json={"body": "missing required fields"},
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            timeout=30,
        )
        assert resp.status_code in (
            400,
            422,
        ), f"Expected 400/422 for missing fields, got {resp.status_code}"
