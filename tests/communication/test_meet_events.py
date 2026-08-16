"""Contract tests for the Recall realtime event relay."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from livekit.protocol.models import DataPacket
from starlette.websockets import WebSocketDisconnect

RELAY_SECRET = "TEST-RELAY-SECRET"


def _client() -> TestClient:
    from communication.meet_events import router

    app = FastAPI()
    app.include_router(router, prefix="/meet")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _relay_secret(monkeypatch):
    monkeypatch.setenv("RECALL_RELAY_SECRET", RELAY_SECRET)


@pytest.fixture
def livekit():
    """Patch the LiveKit client and expose the mock the relay publishes through."""
    fake = MagicMock()
    fake.room.send_data = AsyncMock()
    fake.aclose = AsyncMock()
    with patch("communication.meet_events.get_livekit_api", return_value=fake):
        yield fake


def _sent_payloads(livekit) -> list[dict]:
    return [
        json.loads(call.args[0].data.decode("utf-8"))
        for call in livekit.room.send_data.call_args_list
    ]


def _frame(event: str, participant: dict, body: dict | None = None) -> str:
    """One frame in the shape Recall actually sends.

    Recall wraps the payload twice -- the participant is at
    ``data.data.participant`` -- and a chat message wraps its body a third time
    at ``data.data.data.text``. The relay itself does not care, forwarding
    ``data`` verbatim, but a fixture that invents a shallower shape teaches the
    wrong thing to whoever writes the consumer next: reading one level short
    yields an empty participant, which is what a dead relay also looks like.
    """
    return json.dumps(
        {
            "event": event,
            "data": {
                "data": {
                    "participant": participant,
                    "timestamp": {
                        "absolute": "2026-07-30T10:00:00Z",
                        "relative": 12.5,
                    },
                    "data": body,
                },
                "realtime_endpoint": {"id": "endpoint-1", "metadata": {}},
                "participant_events": {"id": "pe-1", "metadata": {}},
                "recording": {"id": "rec-1", "metadata": {}},
                "bot": {"id": "bot-1", "metadata": {}},
            },
        },
    )


def test_relay_rejects_a_missing_token(livekit) -> None:
    """An unauthenticated socket could publish into any assistant's room."""
    with pytest.raises(WebSocketDisconnect):
        with _client().websocket_connect("/meet/events?room=unity_25_gmeet") as ws:
            ws.receive_text()


def test_relay_rejects_a_wrong_token(livekit) -> None:
    """The shared secret is the only credential Recall can present here."""
    url = "/meet/events?room=unity_25_gmeet&token=not-the-secret"
    with pytest.raises(WebSocketDisconnect):
        with _client().websocket_connect(url) as ws:
            ws.receive_text()


def test_relay_is_disabled_when_no_secret_is_configured(livekit, monkeypatch) -> None:
    """An unset secret must fail closed, not authorize every caller.

    Comparing against "" would otherwise let an empty token in.
    """
    monkeypatch.setenv("RECALL_RELAY_SECRET", "")
    url = "/meet/events?room=unity_25_gmeet&token="
    with pytest.raises(WebSocketDisconnect):
        with _client().websocket_connect(url) as ws:
            ws.receive_text()


def test_relay_publishes_speech_events_into_the_room(livekit) -> None:
    """Speech transitions are what the fast brain attributes utterances with."""
    url = f"/meet/events?room=unity_25_gmeet&token={RELAY_SECRET}"
    with _client().websocket_connect(url) as ws:
        ws.send_text(
            _frame("participant_events.speech_on", {"id": 7, "name": "Ada"}),
        )

    request = livekit.room.send_data.call_args.args[0]
    assert request.room == "unity_25_gmeet"
    assert request.topic == "recall_meeting_events"
    # Lossy delivery would drop speech_off frames, stranding attribution on the
    # previous speaker for the rest of the call.
    assert request.kind == DataPacket.Kind.RELIABLE
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["event"] == "participant_events.speech_on"
    assert payload["data"]["data"]["participant"]["name"] == "Ada"


def test_relay_forwards_the_payload_without_reshaping_it(livekit) -> None:
    """The consumer unwraps; the relay must not quietly flatten en route.

    Trimming here would be a wire-contract change across two repos: the fast
    brain reads ``data.data``, so a relay that forwarded only the inner object
    would strand every event during a rolling deploy.
    """
    url = f"/meet/events?room=unity_25_gmeet&token={RELAY_SECRET}"
    sent = _frame(
        "participant_events.chat_message",
        {"id": 7, "name": "Ada", "email": None},
        {"text": "here is the link", "to": "everyone"},
    )
    with _client().websocket_connect(url) as ws:
        ws.send_text(sent)

    payload = _sent_payloads(livekit)[0]
    assert payload["data"] == json.loads(sent)["data"]
    assert payload["data"]["data"]["data"]["text"] == "here is the link"


def test_relay_skips_events_no_consumer_reads(livekit) -> None:
    """Unread events would put untracked traffic on the data channel all call."""
    url = f"/meet/events?room=unity_25_gmeet&token={RELAY_SECRET}"
    with _client().websocket_connect(url) as ws:
        ws.send_text(json.dumps({"event": "participant_events.webcam_on", "data": {}}))
        ws.send_text(json.dumps({"event": "participant_events.join", "data": {}}))

    events = [payload["event"] for payload in _sent_payloads(livekit)]
    assert events == ["participant_events.join"]


def test_relay_survives_a_malformed_frame(livekit) -> None:
    """Recall reconnects on close, so one bad frame must not drop the stream.

    Tearing down here would lose the events either side of it as well.
    """
    url = f"/meet/events?room=unity_25_gmeet&token={RELAY_SECRET}"
    with _client().websocket_connect(url) as ws:
        ws.send_text("not json at all")
        ws.send_text(json.dumps({"event": "participant_events.leave", "data": {}}))

    events = [payload["event"] for payload in _sent_payloads(livekit)]
    assert events == ["participant_events.leave"]


def test_relay_closes_the_livekit_client_on_disconnect(livekit) -> None:
    """One client per bot; leaking them would exhaust connections over a day."""
    url = f"/meet/events?room=unity_25_gmeet&token={RELAY_SECRET}"
    with _client().websocket_connect(url):
        pass
    livekit.aclose.assert_awaited()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_classify_reports_why_a_frame_was_skipped() -> None:
    """A bare relayed count cannot tell "none sent" from "all dropped".

    Both previous diagnoses of missing inbound chat stalled here: the log said
    one event was forwarded and could not say whether Recall had sent one or
    forty.
    """
    from communication.meet_events import _classify_event

    cases = {
        '{"event": "participant_events.webcam_on", "data": {}}': "not_subscribed",
        "not json at all": "bad_json",
        '{"data": {}}': "no_event_field",
        "[1, 2]": "not_an_object",
    }
    for raw, expected in cases.items():
        classified = _classify_event(raw)
        assert classified.payload is None
        assert classified.skip_reason == expected, raw

    classified = _classify_event(
        _frame(
            "participant_events.chat_message",
            {"id": 7, "name": "Ada"},
            {"text": "hi", "to": "everyone"},
        ),
    )
    assert classified.payload is not None and classified.skip_reason == ""
    assert classified.name == "participant_events.chat_message"


def test_screenshare_transitions_reach_the_room(livekit) -> None:
    """The fast brain starts and stops polling for a shared screen off these.

    Without them it either never looks for a frame, or keeps describing one long
    after the presenter stopped.
    """
    url = f"/meet/events?room=unity_25_gmeet&token={RELAY_SECRET}"
    with _client().websocket_connect(url) as ws:
        ws.send_text(
            _frame("participant_events.screenshare_on", {"id": 7, "name": "Ada"}),
        )
        ws.send_text(
            _frame("participant_events.screenshare_off", {"id": 7, "name": "Ada"}),
        )

    assert [p["event"] for p in _sent_payloads(livekit)] == [
        "participant_events.screenshare_on",
        "participant_events.screenshare_off",
    ]


def test_unsubscribed_events_keep_their_real_name() -> None:
    """Tallying by name is what shows Recall sending something unexpected."""
    from communication.meet_events import _classify_event

    classified = _classify_event(
        '{"event": "participant_events.webcam_on", "data": {}}',
    )
    assert classified.name == "participant_events.webcam_on"
    assert classified.skip_reason == "not_subscribed"


def test_close_tally_survives_an_abrupt_socket_death(livekit) -> None:
    """Cloud Run kills the socket at its request deadline, not cleanly.

    The tally is logged from ``finally`` so a timeout-killed connection still
    reports what it saw -- the previous version logged only on a clean
    WebSocketDisconnect.
    """
    import inspect

    import communication.meet_events as module

    source = inspect.getsource(module.recall_meeting_events)
    finally_block = source.split("finally:", 1)[1]
    assert "recall_relay_closed" in finally_block
    assert '"received"' in finally_block


def test_frame_shape_reports_both_nesting_levels() -> None:
    """Both levels, because every bug here has been reading the wrong one.

    Recall wraps the payload twice -- ``data`` holds artifact references and
    ``data.data`` holds the event -- and a parser reading the wrong level looks
    exactly like a transport that delivered nothing.
    """
    from communication.meet_events import frame_shape

    frame = json.dumps(
        {
            "event": "participant_events.chat_message",
            "data": {
                "bot": {"id": "b"},
                "recording": {"id": "r"},
                "data": {
                    "participant": {"name": "Julia"},
                    "text": "hello",
                    "timestamp": {},
                },
            },
        },
    )
    assert frame_shape(frame) == {
        "data_keys": ["bot", "data", "recording"],
        "inner_keys": ["participant", "text", "timestamp"],
    }


def test_frame_shape_survives_unusable_input() -> None:
    """It runs inside the receive loop; a throw here would kill the relay."""
    from communication.meet_events import frame_shape

    assert frame_shape("not json") == {}
    assert frame_shape(json.dumps({"event": "x"})) == {}
    assert frame_shape(json.dumps({"event": "x", "data": {"data": 5}})) == {
        "data_keys": ["data"],
        "inner_keys": [],
    }
