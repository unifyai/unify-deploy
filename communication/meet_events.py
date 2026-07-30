"""Relay for Recall.ai realtime meeting events into a LiveKit room.

Recall opens one websocket per bot to an endpoint registered at bot creation
and streams in-call events over it -- participant join/leave, speech on/off,
chat. The consumer is the fast brain, which is already a participant in the
assistant's LiveKit room, so this relay republishes each event as a LiveKit
data message rather than routing it through Orchestra: these arrive every
one to three seconds for the length of a call, and trigger resolution is
built for waking an assistant, not for feeding one that is already awake.

What the fast brain does with them is speaker attribution. The platform tells
us *who* is talking, which is the one thing our own transcription cannot
recover, and which today is guessed from scraped DOM state.

Recall cannot set headers on this socket, so the assistant pod puts the room
name and a shared secret in the URL when it creates the bot::

    wss://<comms-host>/meet/events?room=unity_25_gmeet&token=<RECALL_RELAY_SECRET>
"""

from __future__ import annotations

import json
import logging
import secrets
from collections import Counter
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from livekit.api import SendDataRequest
from livekit.protocol.models import DataPacket

from common.livekit import get_livekit_api
from common.settings import SETTINGS

logger = logging.getLogger(__name__)

router = APIRouter()

# LiveKit data topic the fast brain subscribes to for these events.
RECALL_EVENT_TOPIC = "recall_meeting_events"

# Policy violation: the socket authenticated but must not stay open.
_WS_CLOSE_POLICY_VIOLATION = 1008

# Events worth the round trip. Recall emits a wider set (webcam on/off,
# recording permission, breakout rooms) that no consumer reads today; relaying
# them would put unattributed traffic on the room's data channel for the whole
# call. Add to this set when a consumer actually needs one.
_RELAYED_EVENTS = frozenset(
    {
        "participant_events.join",
        "participant_events.leave",
        "participant_events.update",
        "participant_events.speech_on",
        "participant_events.speech_off",
        "participant_events.chat_message",
    },
)


def _authorized(token: str | None) -> bool:
    """Whether a relay token matches the configured shared secret."""

    expected = SETTINGS.recall_relay_secret
    if not expected or not token:
        return False
    return secrets.compare_digest(token, expected)


@router.websocket("/events")
async def recall_meeting_events(
    websocket: WebSocket,
    room: str = Query(...),
    token: str = Query(None),
) -> None:
    """Accept one bot's realtime event stream and fan it into its LiveKit room."""

    if not _authorized(token):
        logger.info({"event": "recall_relay_rejected", "reason": "bad_token"})
        await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()
    logger.info({"event": "recall_relay_opened", "room": room})

    livekit = get_livekit_api()
    # Tallied rather than logged per frame: at 2fps a video subscription would
    # otherwise bury the log. Counting everything received -- not just what is
    # forwarded -- is the point: a bare "relayed: 1" cannot tell "Recall sent
    # one event" apart from "Recall sent forty and we dropped thirty-nine".
    received: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    relayed = 0
    first_seen: set[str] = set()
    try:
        while True:
            raw = await websocket.receive_text()
            payload, event_name, skip_reason = _classify_event(raw)
            received[event_name] += 1

            # One line per event *type*, not per frame: enough to see the shape
            # Recall actually sends without volume scaling with the call.
            if event_name not in first_seen:
                first_seen.add(event_name)
                logger.info(
                    {
                        "event": "recall_relay_first_frame",
                        "room": room,
                        "recall_event": event_name,
                        "data_keys": sorted(skip_reason_data_keys(raw)),
                        "forwarded": payload is not None,
                        "skip_reason": skip_reason or None,
                    },
                )

            if payload is None:
                skipped[skip_reason] += 1
                continue

            await livekit.room.send_data(
                SendDataRequest(
                    room=room,
                    data=json.dumps(payload).encode("utf-8"),
                    # Reliable, not lossy: a dropped speech_off leaves the fast
                    # brain attributing everything after it to the wrong person
                    # for the rest of the call.
                    kind=DataPacket.Kind.RELIABLE,
                    topic=RECALL_EVENT_TOPIC,
                ),
            )
            relayed += 1
    except WebSocketDisconnect:
        pass
    finally:
        # In ``finally`` so the tally survives any exit path, not just a clean
        # disconnect -- a socket killed by a request timeout took the old log
        # line with it.
        logger.info(
            {
                "event": "recall_relay_closed",
                "room": room,
                "relayed": relayed,
                "received": dict(received),
                "skipped": dict(skipped),
            },
        )
        await livekit.aclose()


def skip_reason_data_keys(raw: str) -> list[str]:
    """Top-level keys of a frame's ``data``, for the first-frame log line.

    Shape is the thing worth seeing once per event type: a payload nested a
    level deeper than expected reads as "no events arrived" everywhere
    downstream, which is indistinguishable from a dead transport.
    """
    try:
        message = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(message, dict):
        return []
    data = message.get("data")
    return list(data) if isinstance(data, dict) else []


def _classify_event(raw: str) -> tuple[dict[str, Any] | None, str, str]:
    """Return ``(payload_or_None, event_name, skip_reason)`` for one frame.

    Reports the event name even when skipping, so the caller can tally what
    Recall actually sent rather than only what was forwarded. A malformed or
    unrecognised frame is skipped rather than raised: Recall reconnects on a
    closed socket, so tearing the relay down over one bad frame drops the
    events either side of it too.
    """

    try:
        message = json.loads(raw)
    except ValueError:
        return None, "<bad_json>", "bad_json"
    if not isinstance(message, dict):
        return None, "<not_an_object>", "not_an_object"

    event = message.get("event")
    if not isinstance(event, str) or not event:
        return None, "<no_event_field>", "no_event_field"
    if event not in _RELAYED_EVENTS:
        return None, event, "not_subscribed"

    data = message.get("data")
    return (
        {"event": event, "data": data if isinstance(data, dict) else {}},
        event,
        "",
    )
