from __future__ import annotations

import base64
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
    assert service._messages.marked_read == ["inbound"]
    assert adapter._history_id == "history-start"


def test_gmail_poll_uses_history_after_bootstrap(monkeypatch) -> None:
    service = _FakeGmailService(
        {
            "history-msg": {
                "id": "history-msg",
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


def test_twilio_poll_skips_outbound_messages(monkeypatch) -> None:
    sent = datetime.fromtimestamp(2_000, tz=timezone.utc)
    adapter = bridge.TwilioAdapter("sms", "+15550000000", {"+15551112222"})
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
    monkeypatch.setattr(
        bridge,
        "_post",
        lambda path, envelope: forwarded.append(envelope),
    )

    assert adapter.poll(1_000_000, set()) == 1
    assert len(forwarded) == 1
    assert forwarded[0]["event"]["body"] == "hello"
