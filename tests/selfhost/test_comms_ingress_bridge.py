from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from email.message import EmailMessage
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_BRIDGE_PATH = Path(__file__).parents[2] / "selfhost" / "comms_ingress_bridge.py"
_SPEC = importlib.util.spec_from_file_location("comms_ingress_bridge", _BRIDGE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)


class _Exec:
    def __init__(self, value=None, callback=None):
        self._value = value
        self._callback = callback

    def execute(self):
        if self._callback:
            self._callback()
        return self._value


def _raw_message(*, sender: str, subject: str = "Hello", body: str = "Body") -> str:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "Daniel <dan@unify.ai>"
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


class _FakeGmailMessages:
    def __init__(self, messages: dict[str, dict]):
        self.messages = messages
        self.list_query = None
        self.marked_read: list[str] = []

    def list(self, **kwargs):
        self.list_query = kwargs["q"]
        return _Exec({"messages": [{"id": key} for key in self.messages]})

    def get(self, *, id: str, **_kwargs):
        return _Exec(self.messages[id])

    def modify(self, *, id: str, **_kwargs):
        return _Exec(callback=lambda: self.marked_read.append(id))


class _FakeGmailHistory:
    def __init__(self, response: dict | None = None):
        self.response = response or {"history": [], "historyId": "history-next"}
        self.calls: list[dict] = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _Exec(self.response)


class _FakeGmailService:
    def __init__(
        self,
        messages: dict[str, dict],
        *,
        profile_history_id: str = "history-start",
        history_response: dict | None = None,
    ):
        self._messages = _FakeGmailMessages(messages)
        self._history = _FakeGmailHistory(history_response)
        self._profile_history_id = profile_history_id

    def users(self):
        return self

    def messages(self):
        return self._messages

    def history(self):
        return self._history

    def getProfile(self, **_kwargs):
        return _Exec({"historyId": self._profile_history_id})


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_gmail_poll_skips_self_mail_and_marks_read(monkeypatch) -> None:
    service = _FakeGmailService(
        {
            "self": {
                "id": "self",
                "internalDate": "2000000",
                "labelIds": ["UNREAD"],
                "raw": _raw_message(sender="Twin <local-twin@unify.ai>"),
            },
        },
    )
    adapter = bridge.GmailAdapter()
    adapter._mailbox = "local-twin@unify.ai"
    adapter._service = service
    forwarded = []
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append(envelope),
    )

    assert adapter.poll(1_000_000, set()) == 0
    assert forwarded == []
    assert service._messages.marked_read == ["self"]
    assert "is:unread" in service._messages.list_query
    assert adapter._history_id == "history-start"


def test_gmail_poll_forwards_non_self_mail_with_provider_timestamp(monkeypatch) -> None:
    service = _FakeGmailService(
        {
            "inbound": {
                "id": "inbound",
                "threadId": "gmail-thread-1",
                "internalDate": "2000000",
                "labelIds": ["UNREAD"],
                "raw": _raw_message(sender="Daniel <dan@unify.ai>", body="2001"),
            },
        },
    )
    adapter = bridge.GmailAdapter()
    adapter._mailbox = "local-twin@unify.ai"
    adapter._service = service
    forwarded = []
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append((path, envelope)),
    )

    assert adapter.poll(1_000_000, set()) == 1
    assert forwarded[0][0] == "/local/comms/envelope"
    assert forwarded[0][1]["thread"] == "email"
    assert forwarded[0][1]["publish_timestamp"] == 2000
    assert forwarded[0][1]["event"]["body"].strip() == "2001"
    assert forwarded[0][1]["event"]["thread_id"] == "gmail-thread-1"
    assert forwarded[0][1]["event"]["gmail_message_id"] == "inbound"
    assert service._messages.marked_read == ["inbound"]
    assert adapter._history_id == "history-start"


def test_gmail_poll_uses_history_after_bootstrap(monkeypatch) -> None:
    service = _FakeGmailService(
        {
            "history-msg": {
                "id": "history-msg",
                "threadId": "gmail-thread-history",
                "internalDate": "3000000",
                "labelIds": ["UNREAD"],
                "raw": _raw_message(
                    sender="Daniel <dan@unify.ai>",
                    body="history body",
                ),
            },
        },
        history_response={
            "history": [
                {"messagesAdded": [{"message": {"id": "history-msg"}}]},
            ],
            "historyId": "history-next",
        },
    )
    adapter = bridge.GmailAdapter()
    adapter._mailbox = "local-twin@unify.ai"
    adapter._service = service
    adapter._history_id = "history-start"
    forwarded = []
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append((path, envelope)),
    )

    assert adapter.poll(1_000_000, set()) == 1
    assert service._messages.list_query is None
    assert service._history.calls[0]["startHistoryId"] == "history-start"
    assert adapter._history_id == "history-next"
    assert forwarded[0][1]["event"]["body"].strip() == "history body"
    assert forwarded[0][1]["event"]["thread_id"] == "gmail-thread-history"
    assert forwarded[0][1]["event"]["gmail_message_id"] == "history-msg"


def test_twilio_poll_skips_outbound_messages(monkeypatch) -> None:
    sent = datetime.fromtimestamp(2_000, tz=timezone.utc)
    adapter = bridge.TwilioAdapter("sms", "+15550000000")
    adapter._client = SimpleNamespace(
        messages=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    sid="out",
                    direction="outbound-api",
                    date_sent=sent,
                    date_created=sent,
                    from_="+15551112222",
                    to="+15550000000",
                    body="ignore me",
                ),
                SimpleNamespace(
                    sid="in",
                    direction="inbound",
                    date_sent=sent,
                    date_created=sent,
                    from_="+15551112222",
                    to="+15550000000",
                    body="hello",
                ),
            ],
        ),
    )
    forwarded = []
    gets = []
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("ORCHESTRA_URL", "http://orchestra.test/v0")
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append(envelope),
    )
    monkeypatch.setattr(
        bridge.requests,
        "get",
        lambda url, **kwargs: gets.append((url, kwargs))
        or _Response(payload={"assistant_id": 8, "role": "owner"}),
    )

    assert adapter.poll(1_000_000, set()) == 1
    assert len(forwarded) == 1
    assert forwarded[0]["event"]["body"] == "hello"
    assert forwarded[0]["event"]["assistant_id"] == 8
    assert forwarded[0]["event"]["role"] == "owner"
    assert len(gets) == 1
    assert gets[0][0] == "http://orchestra.test/v0/admin/phone/resolve"
    assert gets[0][1]["params"] == {
        "pool_number": "+15550000000",
        "sender": "+15551112222",
    }


def test_twilio_poll_skips_when_orchestra_returns_action(monkeypatch) -> None:
    sent = datetime.fromtimestamp(2_000, tz=timezone.utc)
    adapter = bridge.TwilioAdapter("sms", "+15550000000")
    adapter._client = SimpleNamespace(
        messages=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    sid="cold",
                    direction="inbound",
                    date_sent=sent,
                    date_created=sent,
                    from_="+15551112222",
                    to="+15550000000",
                    body="hello",
                ),
            ],
        ),
    )
    forwarded = []
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append(envelope),
    )
    monkeypatch.setattr(
        bridge.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload={"action": "reject_cold"}),
    )

    seen = set()
    assert adapter.poll(1_000_000, seen) == 0
    assert forwarded == []
    assert seen == {"cold"}


def test_twilio_poll_skips_when_orchestra_returns_404(monkeypatch) -> None:
    sent = datetime.fromtimestamp(2_000, tz=timezone.utc)
    adapter = bridge.TwilioAdapter("whatsapp", "+447700900001")
    adapter._client = SimpleNamespace(
        messages=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    sid="unknown",
                    direction="inbound",
                    date_sent=sent,
                    date_created=sent,
                    from_="whatsapp:+4915550100009",
                    to="whatsapp:+447700900001",
                    body="hello",
                ),
            ],
        ),
    )
    forwarded = []
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append(envelope),
    )
    monkeypatch.setattr(
        bridge.requests,
        "get",
        lambda *_args, **_kwargs: _Response(404),
    )

    seen = set()
    assert adapter.poll(1_000_000, seen) == 0
    assert forwarded == []
    assert seen == {"unknown"}


def test_twilio_whatsapp_poll_forwards_after_orchestra_resolve(monkeypatch) -> None:
    sent = datetime.fromtimestamp(2_000, tz=timezone.utc)
    adapter = bridge.TwilioAdapter("whatsapp", "+447700900001")
    adapter._client = SimpleNamespace(
        messages=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    sid="wa-inbound",
                    direction="inbound",
                    date_sent=sent,
                    date_created=sent,
                    from_="whatsapp:+4915550100009",
                    to="whatsapp:+447700900001",
                    body="hello",
                ),
            ],
        ),
    )
    forwarded = []
    gets = []
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("ORCHESTRA_URL", "http://orchestra.test/v0")
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append((path, envelope)),
    )
    monkeypatch.setattr(
        bridge.requests,
        "get",
        lambda url, **kwargs: gets.append((url, kwargs))
        or _Response(payload={"assistant_id": 8, "role": "owner"}),
    )

    assert adapter.poll(1_000_000, set()) == 1

    assert gets == [
        (
            "http://orchestra.test/v0/admin/whatsapp/resolve",
            {
                "params": {
                    "pool_number": "+447700900001",
                    "sender": "+4915550100009",
                },
                "headers": {"Authorization": "Bearer admin-key"},
                "timeout": 10,
            },
        ),
    ]
    assert forwarded == [
        (
            "/local/comms/envelope",
            {
                "thread": "whatsapp",
                "event": {
                    "contacts": [],
                    "from_number": "+4915550100009",
                    "to_number": "+447700900001",
                    "body": "hello",
                    "attachments": [],
                    "assistant_id": 8,
                    "role": "owner",
                },
            },
        ),
    ]


def test_twilio_poll_skips_without_admin_key(monkeypatch) -> None:
    sent = datetime.fromtimestamp(2_000, tz=timezone.utc)
    adapter = bridge.TwilioAdapter("sms", "+15550000000")
    adapter._client = SimpleNamespace(
        messages=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    sid="no-key",
                    direction="inbound",
                    date_sent=sent,
                    date_created=sent,
                    from_="+15551112222",
                    to="+15550000000",
                    body="hello",
                ),
            ],
        ),
    )
    forwarded = []
    monkeypatch.delenv("ORCHESTRA_ADMIN_KEY", raising=False)
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append(envelope),
    )

    seen = set()
    assert adapter.poll(1_000_000, seen) == 0
    assert forwarded == []
    assert seen == {"no-key"}


def test_twilio_poll_skips_when_resolve_raises(monkeypatch) -> None:
    sent = datetime.fromtimestamp(2_000, tz=timezone.utc)
    adapter = bridge.TwilioAdapter("sms", "+15550000000")
    adapter._client = SimpleNamespace(
        messages=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    sid="resolve-error",
                    direction="inbound",
                    date_sent=sent,
                    date_created=sent,
                    from_="+15551112222",
                    to="+15550000000",
                    body="hello",
                ),
            ],
        ),
    )
    forwarded = []
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append(envelope),
    )

    def raise_get(*_args, **_kwargs):
        raise RuntimeError("orchestra down")

    monkeypatch.setattr(bridge.requests, "get", raise_get)

    seen = set()
    assert adapter.poll(1_000_000, seen) == 0
    assert forwarded == []
    assert seen == {"resolve-error"}


def test_twilio_whatsapp_permission_response_updates_orchestra(
    monkeypatch,
    tmp_path,
) -> None:
    sent = datetime.fromtimestamp(2_000, tz=timezone.utc)
    adapter = bridge.TwilioAdapter("whatsapp", "+447700900001")
    adapter._client = SimpleNamespace(
        messages=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    sid="wa-permission",
                    direction="inbound",
                    date_sent=sent,
                    date_created=sent,
                    from_="whatsapp:+4915550100009",
                    to="whatsapp:+447700900001",
                    body="VOICE_CALL_REQUEST",
                    button_payload="ACCEPTED",
                ),
            ],
        ),
    )
    forwarded = []
    posts = []
    gets = []

    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("ORCHESTRA_URL", "http://orchestra.test/v0")
    monkeypatch.setenv(
        "COMMS_BRIDGE_PERMISSION_CACHE",
        str(tmp_path / "wa-permissions.json"),
    )
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append((path, envelope)),
    )

    def fake_post(url, **kwargs):
        posts.append((url, kwargs))
        return SimpleNamespace(raise_for_status=lambda: None)

    def fake_get(url, **kwargs):
        gets.append((url, kwargs))
        return _Response(payload={"assistant_id": 8, "role": "owner"})

    monkeypatch.setattr(bridge.requests, "post", fake_post)
    monkeypatch.setattr(bridge.requests, "get", fake_get)

    assert adapter.poll(1_000_000, set()) == 1

    assert posts == [
        (
            "http://orchestra.test/v0/admin/whatsapp/call-permission",
            {
                "headers": {"Authorization": "Bearer admin-key"},
                "json": {
                    "pool_number": "+447700900001",
                    "contact_number": "+4915550100009",
                    "status": "accepted",
                    "source": "selfhost_bridge",
                },
                "timeout": 10,
            },
        ),
    ]
    assert forwarded == [
        (
            "/local/comms/envelope",
            {
                "thread": "whatsapp",
                "event": {
                    "contacts": [],
                    "type": "call_permission_response",
                    "contact_number": "+4915550100009",
                    "from_number": "+4915550100009",
                    "to_number": "+447700900001",
                    "body": "VOICE_CALL_REQUEST",
                    "payload": "ACCEPTED",
                    "attachments": [],
                    "assistant_id": 8,
                    "role": "owner",
                },
            },
        ),
    ]
    assert gets[0][0] == "http://orchestra.test/v0/admin/whatsapp/resolve"


def test_twilio_whatsapp_permission_rejection_updates_orchestra_and_forwards_event(
    monkeypatch,
    tmp_path,
) -> None:
    sent = datetime.fromtimestamp(2_000, tz=timezone.utc)
    adapter = bridge.TwilioAdapter("whatsapp", "+447700900001")
    adapter._client = SimpleNamespace(
        messages=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    sid="wa-rejected-permission",
                    direction="inbound",
                    date_sent=sent,
                    date_created=sent,
                    from_="whatsapp:+4915550100009",
                    to="whatsapp:+447700900001",
                    body="VOICE_CALL_REQUEST",
                    button_payload="REJECTED",
                ),
            ],
        ),
    )
    forwarded = []
    posts = []
    gets = []
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("ORCHESTRA_URL", "http://orchestra.test/v0")
    monkeypatch.setenv(
        "COMMS_BRIDGE_PERMISSION_CACHE",
        str(tmp_path / "wa-permissions.json"),
    )
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append((path, envelope)),
    )
    monkeypatch.setattr(
        bridge.requests,
        "post",
        lambda url, **kwargs: posts.append((url, kwargs))
        or SimpleNamespace(raise_for_status=lambda: None),
    )
    monkeypatch.setattr(
        bridge.requests,
        "get",
        lambda url, **kwargs: gets.append((url, kwargs))
        or _Response(payload={"assistant_id": 8, "role": "owner"}),
    )

    assert adapter.poll(1_000_000, set()) == 1

    assert posts[0][1]["json"]["status"] == "rejected"
    assert posts[0][1]["json"]["source"] == "selfhost_bridge"
    assert forwarded[0][1]["event"]["type"] == "call_permission_response"
    assert forwarded[0][1]["event"]["payload"] == "REJECTED"
    assert forwarded[0][1]["event"]["contact_number"] == "+4915550100009"
    assert forwarded[0][1]["event"]["assistant_id"] == 8
    assert forwarded[0][1]["event"]["role"] == "owner"
    assert gets[0][0] == "http://orchestra.test/v0/admin/whatsapp/resolve"


def test_twilio_whatsapp_permission_without_payload_is_unknown_interaction(
    monkeypatch,
) -> None:
    sent = datetime.fromtimestamp(2_000, tz=timezone.utc)
    adapter = bridge.TwilioAdapter("whatsapp", "+447700900001")
    adapter._client = SimpleNamespace(
        messages=SimpleNamespace(
            list=lambda **_kwargs: [
                SimpleNamespace(
                    sid="wa-missing-permission-payload",
                    direction="inbound",
                    date_sent=sent,
                    date_created=sent,
                    from_="whatsapp:+4915550100009",
                    to="whatsapp:+447700900001",
                    body="VOICE_CALL_REQUEST",
                ),
            ],
        ),
    )
    forwarded = []
    posts = []
    gets = []
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("ORCHESTRA_URL", "http://orchestra.test/v0")
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append((path, envelope)),
    )
    monkeypatch.setattr(
        bridge.requests,
        "post",
        lambda url, **kwargs: posts.append((url, kwargs))
        or SimpleNamespace(raise_for_status=lambda: None),
    )
    monkeypatch.setattr(
        bridge.requests,
        "get",
        lambda url, **kwargs: gets.append((url, kwargs))
        or _Response(payload={"assistant_id": 8, "role": "owner"}),
    )

    assert adapter.poll(1_000_000, set()) == 1

    assert posts[0][1]["json"] == {
        "pool_number": "+447700900001",
        "contact_number": "+4915550100009",
        "status": "unknown_interaction",
        "source": "selfhost_bridge",
    }
    assert forwarded[0][1]["event"]["type"] == "call_permission_response"
    assert forwarded[0][1]["event"]["payload"] == "UNKNOWN"
    assert forwarded[0][1]["event"]["contact_number"] == "+4915550100009"
    assert gets[0][0] == "http://orchestra.test/v0/admin/whatsapp/resolve"


def test_whatsapp_permission_cache_hydrates_orchestra(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "wa-permissions.json"
    cache_path.write_text(
        json.dumps(
            {
                "+447700900001|+4915550100009": {
                    "pool_number": "+447700900001",
                    "contact_number": "+4915550100009",
                    "status": "accepted",
                    "expires_at": "2999-01-01T00:00:00+00:00",
                    "updated_at": "2026-06-25T16:00:00+00:00",
                },
            },
        ),
        encoding="utf-8",
    )
    posts = []
    monkeypatch.setenv("ORCHESTRA_ADMIN_KEY", "admin-key")
    monkeypatch.setenv("ORCHESTRA_URL", "http://orchestra.test/v0")
    monkeypatch.setenv("COMMS_BRIDGE_PERMISSION_CACHE", str(cache_path))
    monkeypatch.setattr(
        bridge.requests,
        "post",
        lambda url, **kwargs: posts.append((url, kwargs))
        or SimpleNamespace(raise_for_status=lambda: None),
    )

    bridge._hydrate_call_permission_cache()

    assert posts == [
        (
            "http://orchestra.test/v0/admin/whatsapp/call-permission",
            {
                "headers": {"Authorization": "Bearer admin-key"},
                "json": {
                    "pool_number": "+447700900001",
                    "contact_number": "+4915550100009",
                    "status": "accepted",
                    "source": "selfhost_bridge_cache",
                },
                "timeout": 10,
            },
        ),
    ]
