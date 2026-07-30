"""Contract tests for the Recall realtime event relay."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from livekit.protocol.models import DataPacket
from starlette.websockets import WebSocketDisconnect

from common.settings import SETTINGS

RELAY_SECRET = "TEST-RELAY-SECRET"


def _client() -> TestClient:
    from communication.meet_events import router

    app = FastAPI()
    app.include_router(router, prefix="/meet")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _relay_secret():
    previous = SETTINGS.recall_relay_secret
    SETTINGS.recall_relay_secret = RELAY_SECRET
    yield
    SETTINGS.recall_relay_secret = previous


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


def test_relay_is_disabled_when_no_secret_is_configured(livekit) -> None:
    """An unset secret must fail closed, not authorize every caller.

    Comparing against "" would otherwise let an empty token in.
    """
    SETTINGS.recall_relay_secret = ""
    url = "/meet/events?room=unity_25_gmeet&token="
    with pytest.raises(WebSocketDisconnect):
        with _client().websocket_connect(url) as ws:
            ws.receive_text()


def test_relay_publishes_speech_events_into_the_room(livekit) -> None:
    """Speech transitions are what the fast brain attributes utterances with."""
    url = f"/meet/events?room=unity_25_gmeet&token={RELAY_SECRET}"
    with _client().websocket_connect(url) as ws:
        ws.send_text(
            json.dumps(
                {
                    "event": "participant_events.speech_on",
                    "data": {"participant": {"id": 7, "name": "Ada"}},
                },
            ),
        )

    request = livekit.room.send_data.call_args.args[0]
    assert request.room == "unity_25_gmeet"
    assert request.topic == "recall_meeting_events"
    # Lossy delivery would drop speech_off frames, stranding attribution on the
    # previous speaker for the rest of the call.
    assert request.kind == DataPacket.Kind.RELIABLE
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["event"] == "participant_events.speech_on"
    assert payload["data"]["participant"]["name"] == "Ada"


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
        payload, _, reason = _classify_event(raw)
        assert payload is None
        assert reason == expected, raw

    payload, name, reason = _classify_event(
        '{"event": "participant_events.chat_message", "data": {"data": {"text": "hi"}}}',
    )
    assert payload is not None and reason == ""
    assert name == "participant_events.chat_message"


def test_unsubscribed_events_keep_their_real_name() -> None:
    """Tallying by name is what shows Recall sending something unexpected."""
    from communication.meet_events import _classify_event

    _, name, reason = _classify_event(
        '{"event": "participant_events.screenshare_on", "data": {}}',
    )
    assert name == "participant_events.screenshare_on"
    assert reason == "not_subscribed"


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
