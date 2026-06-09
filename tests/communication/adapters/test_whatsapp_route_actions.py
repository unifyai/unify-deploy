"""WhatsApp adapter route-action tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from adapters import main
from common.livekit import make_call_scoped_sip_uri


def test_twilio_whatsapp_reject_ambiguous_returns_closed_response(monkeypatch):
    monkeypatch.setattr(
        main,
        "resolve_whatsapp_route",
        lambda pool_number, sender: {"action": "reject_ambiguous"},
    )
    main.app.dependency_overrides[main.validate_twilio_wa_signature] = lambda: None
    try:
        with TestClient(main.app) as client:
            response = client.post(
                "/twilio/whatsapp",
                data={
                    "To": "whatsapp:+15550800001",
                    "From": "whatsapp:+15550800002",
                    "Body": "Hello",
                },
            )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "text/xml" in response.headers["content-type"]
    assert "This number is not accepting new messages." in response.text


class _FakePublishFuture:
    def result(self, timeout=None):
        return "message-id"


class _FakePubSub:
    def __init__(self):
        self.published = []

    def topic_path(self, project_id, topic_name):
        return f"projects/{project_id}/topics/{topic_name}"

    def publish(self, topic_path, payload, **attrs):
        self.published.append((topic_path, payload, attrs))
        return _FakePublishFuture()


def _install_whatsapp_call_stubs(monkeypatch):
    published = _FakePubSub()
    sessions = []

    monkeypatch.setattr(
        main,
        "resolve_whatsapp_route",
        lambda pool_number, sender: {
            "assistant_id": 101 if sender.endswith("01") else 202,
            "role": "owner",
        },
    )

    def fake_context(_medium, _destination, _sender, *, assistant_id, **_kwargs):
        return {
            "assistant": {
                "assistant_id": str(assistant_id),
                "user_id": f"user-{assistant_id}",
            },
            "contacts": [{"contact_id": 1, "whatsapp_number": _sender}],
        }

    monkeypatch.setattr(main, "build_webhook_context", fake_context)
    monkeypatch.setattr(main, "get_pubsub_client", lambda: published)
    monkeypatch.setattr(
        main,
        "ensure_call_scoped_dispatch_rule",
        AsyncMock(side_effect=lambda **kwargs: f"rule-{kwargs['call_id']}"),
    )
    monkeypatch.setattr(main, "start_room_egress", AsyncMock())
    monkeypatch.setattr(
        main,
        "upsert_whatsapp_call_session",
        lambda payload: sessions.append(payload) or payload,
    )

    class FakeCalls:
        def create(self, **kwargs):
            return type("Call", (), {"sid": f"SIP-{kwargs['to']}"})()

    monkeypatch.setattr(
        main,
        "get_twilio_wa_client",
        lambda: type("Client", (), {"calls": FakeCalls()})(),
    )
    return published, sessions


def test_twilio_whatsapp_call_uses_per_call_sessions(monkeypatch):
    _published, sessions = _install_whatsapp_call_stubs(monkeypatch)
    main.app.dependency_overrides[main.validate_twilio_wa_signature] = lambda: None
    try:
        with TestClient(main.app) as client:
            first = client.post(
                "/twilio/whatsapp-call",
                data={
                    "To": "whatsapp:+15550800000",
                    "From": "whatsapp:+15550000001",
                    "CallSid": "CA111",
                },
            )
            second = client.post(
                "/twilio/whatsapp-call",
                data={
                    "To": "whatsapp:+15550800000",
                    "From": "whatsapp:+15550000002",
                    "CallSid": "CA222",
                },
            )
    finally:
        main.app.dependency_overrides.clear()

    assert first.status_code == 200
    assert second.status_code == 200
    assert [session["provider_call_sid"] for session in sessions] == ["CA111", "CA222"]
    assert sessions[0]["livekit_room"] == "unity_wa_room_101_CA111"
    assert sessions[1]["livekit_room"] == "unity_wa_room_202_CA222"
    assert sessions[0]["conference_name"] != sessions[1]["conference_name"]


def test_twilio_whatsapp_status_uses_call_session(monkeypatch):
    monkeypatch.setattr(
        main,
        "resolve_whatsapp_route",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("rerouted")),
    )
    monkeypatch.setattr(
        main,
        "get_whatsapp_call_session",
        lambda provider_call_sid: {
            "provider_call_sid": provider_call_sid,
            "assistant_id": 101,
            "from_number": "+15550000001",
            "to_number": "+15550800000",
            "conference_name": "unity_wa_conf_CA111",
            "livekit_room": "unity_wa_room_101_CA111",
            "metadata": {"sip_dispatch_rule_id": "rule-CA111"},
        },
    )
    updated = []
    monkeypatch.setattr(
        main,
        "update_whatsapp_call_session",
        lambda payload: updated.append(payload) or payload,
    )
    monkeypatch.setattr(main, "delete_sip_dispatch_rule", AsyncMock())
    monkeypatch.setattr(
        main,
        "build_webhook_context",
        lambda *_args, **_kwargs: {
            "assistant": {"assistant_id": "101"},
            "contacts": [{"contact_id": 1, "whatsapp_number": "+15550000001"}],
        },
    )
    published = _FakePubSub()
    monkeypatch.setattr(main, "get_pubsub_client", lambda: published)
    main.app.dependency_overrides[main.validate_twilio_wa_signature] = lambda: None
    try:
        with TestClient(main.app) as client:
            response = client.post(
                "/twilio/whatsapp-call-status",
                data={"CallSid": "CA111", "CallStatus": "in-progress"},
            )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert updated[0]["provider_call_sid"] == "CA111"
    assert len(published.published) == 1


def test_call_scoped_sip_uri_uses_unique_target_and_headers(monkeypatch):
    monkeypatch.setenv("LIVEKIT_SIP_URI", "tenant.sip.livekit.cloud")

    uri, sip_target = make_call_scoped_sip_uri(
        "+15550800000",
        "CA:111",
        headers={
            "Unity-Call-Session": "CA-111",
            "X-Unity-Room": "unity_wa_room_101_CA-111",
        },
    )

    assert sip_target == "15550800000-CA-111"
    assert uri.startswith("sip:15550800000-CA-111@tenant.sip.livekit.cloud?")
    assert "X-Unity-Call-Session=CA-111" in uri
    assert "X-Unity-Room=unity_wa_room_101_CA-111" in uri


def test_twilio_whatsapp_completed_status_cleans_rule_without_publish(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_whatsapp_call_session",
        lambda provider_call_sid: {
            "provider_call_sid": provider_call_sid,
            "assistant_id": 101,
            "from_number": "+15550000001",
            "to_number": "+15550800000",
            "conference_name": "unity_wa_conf_CA111",
            "livekit_room": "unity_wa_room_101_CA111",
            "metadata": {"sip_dispatch_rule_id": "rule-CA111"},
        },
    )
    updated = []
    monkeypatch.setattr(
        main,
        "update_whatsapp_call_session",
        lambda payload: updated.append(payload) or payload,
    )
    delete_rule = AsyncMock()
    monkeypatch.setattr(main, "delete_sip_dispatch_rule", delete_rule)
    published = _FakePubSub()
    monkeypatch.setattr(main, "get_pubsub_client", lambda: published)
    monkeypatch.setattr(
        main,
        "build_webhook_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("completed status should not publish"),
        ),
    )
    main.app.dependency_overrides[main.validate_twilio_wa_signature] = lambda: None
    try:
        with TestClient(main.app) as client:
            response = client.post(
                "/twilio/whatsapp-call-status",
                data={"CallSid": "CA111", "CallStatus": "completed"},
            )
    finally:
        main.app.dependency_overrides.clear()

    assert response.status_code == 200
    assert updated == [
        {
            "provider": "twilio",
            "provider_call_sid": "CA111",
            "status": "completed",
        },
    ]
    delete_rule.assert_awaited_once_with("rule-CA111")
    assert published.published == []


def test_recording_complete_updates_session_and_publishes_session_fields(monkeypatch):
    file_result = SimpleNamespace(filename="recordings/call.mp3", size=1234)
    egress_info = SimpleNamespace(
        egress_id="egress-1",
        room_name="unity_wa_room_101_CA111",
        status="EGRESS_COMPLETE",
        file_results=[file_result],
    )
    event = SimpleNamespace(event="egress_ended", egress_info=egress_info)
    monkeypatch.setattr(main, "verify_livekit_webhook", lambda _body, _auth: event)
    monkeypatch.setattr(
        main,
        "build_webhook_context",
        lambda *_args, **_kwargs: {
            "assistant": {"assistant_id": "101"},
            "contacts": [],
            "is_job_running": False,
        },
    )
    updated = []
    monkeypatch.setattr(
        main,
        "update_whatsapp_call_session",
        lambda payload: updated.append(payload) or payload,
    )
    published = _FakePubSub()
    monkeypatch.setattr(main, "get_pubsub_client", lambda: published)

    with TestClient(main.app) as client:
        response = client.post(
            "/livekit/recording-complete"
            "?assistant_id=101"
            "&user_id=user-101"
            "&room_name=unity_wa_room_101_CA111"
            "&call_session_id=CA111"
            "&provider_call_sid=CA111"
            "&conference_name=unity_wa_conf_CA111",
            content="{}",
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200
    assert updated[0]["provider_call_sid"] == "CA111"
    assert updated[0]["status"] == "recording_ready"
    assert updated[0]["metadata"]["recording_room_name"] == "unity_wa_room_101_CA111"
    assert len(published.published) == 1
    payload = json.loads(published.published[0][1].decode("utf-8"))
    assert payload["thread"] == "recording_ready"
    assert payload["event"]["call_session_id"] == "CA111"
    assert payload["event"]["provider_call_sid"] == "CA111"
    assert payload["event"]["conference_name"] == "unity_wa_conf_CA111"
    assert payload["event"]["room_name"] == "unity_wa_room_101_CA111"
    assert payload["event"]["livekit_room"] == "unity_wa_room_101_CA111"
