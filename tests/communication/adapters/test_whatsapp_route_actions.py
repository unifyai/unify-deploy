"""WhatsApp adapter route-action tests."""

from __future__ import annotations

import json
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from livekit.protocol.egress import EgressStatus

from adapters import main
from common.livekit import make_call_scoped_sip_uri


def _gmail_envelope(email_address="twin@unify.ai", history_id="hist-1"):
    data = base64.b64encode(
        json.dumps(
            {
                "emailAddress": email_address,
                "historyId": history_id,
            },
        ).encode("utf-8"),
    ).decode("ascii")
    return {"message": {"data": data}}


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


def _install_shared_gmail_stubs(monkeypatch, *, route):
    published = []
    contexts = []
    monkeypatch.setenv("GCP_SA_KEY", "{}")
    monkeypatch.setattr(
        main.Credentials,
        "from_service_account_info",
        staticmethod(lambda *_args, **_kwargs: object()),
    )
    monkeypatch.setattr(main, "build", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        main,
        "get_assistant",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("legacy lookup")),
    )
    monkeypatch.setattr(main, "resolve_email_route", lambda *_args: route)
    monkeypatch.setattr(
        main,
        "get_thread_id",
        lambda *_args: (
            "thread-1",
            "email-1",
            {
                "sender": "Owner <owner@example.com>",
                "to": "twin@unify.ai",
                "cc": "",
                "bcc": "",
                "subject": "Hello",
                "content": "Body",
            },
            "gmail-message-1",
        ),
    )

    def fake_context(channel, destination, sender, *, assistant_id, validate_contact):
        contexts.append(
            {
                "channel": channel,
                "destination": destination,
                "sender": sender,
                "assistant_id": assistant_id,
                "validate_contact": validate_contact,
            },
        )
        return {
            "assistant": {"assistant_id": assistant_id, "user_id": "user-1"},
            "contacts": [{"contact_id": 1, "email": sender}],
            "is_valid_contact": True,
            "is_job_running": False,
        }

    monkeypatch.setattr(main, "build_webhook_context", fake_context)
    monkeypatch.setattr(
        main,
        "publish_gmail_thread_id",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )
    return published, contexts


def test_shared_gmail_notification_resolves_owner_and_publishes(monkeypatch):
    published, contexts = _install_shared_gmail_stubs(
        monkeypatch,
        route={"assistant_id": 101, "role": "owner"},
    )

    response = main.gmail_notification_processor(_gmail_envelope())

    assert response.status_code == 200
    assert contexts == [
        {
            "channel": "email",
            "destination": "twin@unify.ai",
            "sender": "owner@example.com",
            "assistant_id": "101",
            "validate_contact": False,
        },
    ]
    assert published[0][0][0] == "101"
    assert published[0][1]["shared_mailbox"] == "twin@unify.ai"


def test_shared_gmail_notification_reject_action_does_not_publish(monkeypatch):
    published, contexts = _install_shared_gmail_stubs(
        monkeypatch,
        route={"action": "reject_ambiguous"},
    )

    response = main.gmail_notification_processor(_gmail_envelope())

    assert response.status_code == 200
    assert contexts == []
    assert published == []


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


def _install_phone_call_stubs(monkeypatch):
    published = _FakePubSub()
    sessions = []

    monkeypatch.setattr(
        main,
        "resolve_phone_route",
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
            "contacts": [{"contact_id": 1, "phone_number": _sender}],
            "is_valid_contact": True,
            "is_job_running": False,
        }

    monkeypatch.setattr(main, "build_webhook_context", fake_context)
    monkeypatch.setattr(main, "get_pubsub_client", lambda: published)
    monkeypatch.setattr(
        main,
        "ensure_call_scoped_dispatch_rule",
        AsyncMock(side_effect=lambda **kwargs: f"rule-{kwargs['call_id']}"),
    )
    monkeypatch.setattr(main, "add_user_to_conference", lambda *_args: "sip-leg")
    monkeypatch.setattr(
        main,
        "upsert_phone_call_session",
        lambda payload: sessions.append(payload) or payload,
    )
    return published, sessions


def test_twilio_phone_call_uses_per_call_sessions(monkeypatch):
    _published, sessions = _install_phone_call_stubs(monkeypatch)
    main.app.dependency_overrides[main.validate_twilio_signature] = lambda: None
    try:
        with TestClient(main.app) as client:
            first = client.post(
                "/twilio/call",
                data={
                    "To": "+15550800000",
                    "From": "+15550000001",
                    "CallSid": "CA111",
                },
            )
            second = client.post(
                "/twilio/call",
                data={
                    "To": "+15550800000",
                    "From": "+15550000002",
                    "CallSid": "CA222",
                },
            )
    finally:
        main.app.dependency_overrides.clear()

    assert first.status_code == 200
    assert second.status_code == 200
    assert [session["provider_call_sid"] for session in sessions] == ["CA111", "CA222"]
    assert sessions[0]["livekit_room"] == "unity_phone_room_101_CA111"
    assert sessions[1]["livekit_room"] == "unity_phone_room_202_CA222"
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


# 2026-07-27T12:48:26Z in nanoseconds, matching LiveKit's EgressInfo.started_at.
EGRESS_STARTED_AT_NS = 1785156506000000000


def _egress_ended_event(
    *,
    status=EgressStatus.EGRESS_COMPLETE,
    size=1234,
    error="",
    file_results=None,
    started_at=EGRESS_STARTED_AT_NS,
):
    """An ``egress_ended`` webhook event shaped like LiveKit's.

    ``status`` is the protobuf enum, not its name -- comparing against the name
    silently treats every completed egress as failed.
    """
    if file_results is None:
        file_results = [SimpleNamespace(filename="recordings/call.mp3", size=size)]
    egress_info = SimpleNamespace(
        egress_id="egress-1",
        room_name="unity_wa_room_101_CA111",
        status=status,
        error=error,
        file_results=file_results,
        started_at=started_at,
    )
    return SimpleNamespace(event="egress_ended", egress_info=egress_info)


def test_recording_complete_updates_session_and_publishes_session_fields(monkeypatch):
    event = _egress_ended_event()
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
    # The handler offers every recording to the phone session first and only
    # falls back to WhatsApp when that finds nothing. A WhatsApp room has no
    # phone session, so None is what production answers here.
    monkeypatch.setattr(main, "update_phone_call_session", lambda _payload: None)

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
    # t=0 of the audio, so consumers can time-align utterances against the file
    # rather than against the call-started event a few seconds earlier.
    assert payload["event"]["recording_started_at"] == "2026-07-27T12:48:26+00:00"


def test_recording_complete_omits_the_anchor_when_livekit_does_not_report_it(
    monkeypatch,
):
    """No start time is better than a wrong one: consumers fall back."""
    event = _egress_ended_event(started_at=0)
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
    monkeypatch.setattr(main, "update_phone_call_session", lambda _payload: None)
    monkeypatch.setattr(main, "update_whatsapp_call_session", lambda payload: payload)
    published = _FakePubSub()
    monkeypatch.setattr(main, "get_pubsub_client", lambda: published)

    with TestClient(main.app) as client:
        response = client.post(
            "/livekit/recording-complete?assistant_id=101&provider_call_sid=CA111",
            content="{}",
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200
    payload = json.loads(published.published[0][1].decode("utf-8"))
    assert payload["event"]["recording_started_at"] == ""


def _assert_recording_complete_drops(monkeypatch, event):
    """Post an egress_ended event and assert nothing is linked or published."""
    monkeypatch.setattr(main, "verify_livekit_webhook", lambda _body, _auth: event)
    monkeypatch.setattr(
        main,
        "build_webhook_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a recording that does not exist must not wake the job"),
        ),
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
            "&room_name=unity_wa_room_101_CA111"
            "&provider_call_sid=CA111",
            content="{}",
            headers={"Authorization": "Bearer test"},
        )

    assert response.status_code == 200
    assert published.published == []
    assert updated == []


def test_recording_complete_drops_aborted_egress(monkeypatch):
    """An aborted egress wrote no file, so there is nothing to link.

    This is the observed production shape: egress started before the room had a
    publishing participant, waited, then aborted with "Start signal not
    received". Publishing it would attach a URL that 404s.
    """
    _assert_recording_complete_drops(
        monkeypatch,
        _egress_ended_event(
            status=EgressStatus.EGRESS_ABORTED,
            error="Start signal not received",
        ),
    )


def test_recording_complete_drops_failed_egress(monkeypatch):
    _assert_recording_complete_drops(
        monkeypatch,
        _egress_ended_event(
            status=EgressStatus.EGRESS_FAILED,
            error="upload failed",
        ),
    )


def test_recording_complete_drops_empty_file(monkeypatch):
    """A named object with zero bytes is not a recording."""
    _assert_recording_complete_drops(
        monkeypatch,
        _egress_ended_event(size=0),
    )


def test_recording_complete_drops_egress_without_file_results(monkeypatch):
    _assert_recording_complete_drops(
        monkeypatch,
        _egress_ended_event(file_results=[]),
    )


def test_scheduled_email_watches_renews_shared_mailbox_once(monkeypatch):
    posted = []

    class FakeGetResponse:
        status_code = 200

        def json(self):
            return {
                "info": [
                    {
                        "email": "twin@unify.ai",
                        "email_provider": "google_workspace",
                        "secrets": {},
                    },
                    {
                        "email": "twin@unify.ai",
                        "email_provider": "google_workspace",
                        "secrets": {},
                    },
                    {
                        "email": "alice@example.com",
                        "email_provider": "google_workspace",
                        "secrets": {},
                    },
                ],
            }

    class FakePostResponse:
        def json(self):
            return {"success": True}

    monkeypatch.setattr(main.SETTINGS, "orchestra_admin_key", "admin")
    monkeypatch.setattr(
        main.requests,
        "get",
        lambda *_args, **_kwargs: FakeGetResponse(),
    )

    def fake_post(_url, *, json, **_kwargs):
        posted.append(json)
        return FakePostResponse()

    monkeypatch.setattr(main.requests, "post", fake_post)

    result = main.scheduled_email_watches(main.ScheduledPayload(test=False))

    primary_emails = [payload["primary_email"] for payload in posted]
    assert primary_emails.count("twin@unify.ai") == 1
    assert "alice@example.com" in primary_emails
    assert result["gmail"][0]["email"] == "alice@example.com"
    assert any(row["email"] == "twin@unify.ai" for row in result["gmail"])
