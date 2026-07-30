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
from typing import Any, NamedTuple

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from livekit.api import SendDataRequest
from livekit.protocol.models import DataPacket

from common.livekit import get_livekit_api
from common.settings import SETTINGS
from communication.meet_screenshare import ScreenshareRelay

logger = logging.getLogger(__name__)

router = APIRouter()

# LiveKit data topic the fast brain subscribes to for these events.
RECALL_EVENT_TOPIC = "recall_meeting_events"

# Policy violation: the socket authenticated but must not stay open.
_WS_CLOSE_POLICY_VIOLATION = 1008

# Named because the relay acts on them as well as forwarding them: they are what
# orders several presenters so the frame store knows whose screen is focused.
EVENT_SCREENSHARE_ON = "participant_events.screenshare_on"
EVENT_SCREENSHARE_OFF = "participant_events.screenshare_off"

# Events worth the round trip. Recall emits a wider set (webcam on/off,
# recording permission, breakout rooms) that no consumer reads today; relaying
# them would put unattributed traffic on the room's data channel for the whole
# call. Add to this set when a consumer actually needs one.
#
# MIRRORED: ``SUBSCRIBED_EVENTS`` in unify's
# ``conversation_manager/domains/recall/events.py`` is what a bot is told to
# send. Anything absent from this copy is subscribed and then silently dropped
# in transit. Change both together.
_RELAYED_EVENTS = frozenset(
    {
        "participant_events.join",
        "participant_events.leave",
        "participant_events.update",
        "participant_events.speech_on",
        "participant_events.speech_off",
        "participant_events.chat_message",
        EVENT_SCREENSHARE_ON,
        EVENT_SCREENSHARE_OFF,
    },
)

# Per-participant video frames. Handled here rather than relayed: the frames go
# to GCS for the assistant pod to poll (see ``meet_screenshare.py``), because a
# 360p JPEG does not belong on a reliable data channel. Screenshare and webcam
# frames share this one event and are told apart only by ``type``.
_VIDEO_FRAME_EVENT = "video_separate_png.data"
_VIDEO_FRAME_TYPE_SCREENSHARE = "screenshare"


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
    screenshare = ScreenshareRelay(room=room)
    try:
        while True:
            raw = await websocket.receive_text()
            payload, event_name, skip_reason, message = _classify_event(raw)
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
                        **frame_shape(raw),
                        "forwarded": payload is not None,
                        "skip_reason": skip_reason or None,
                    },
                )

            # Video frames leave by their own route: too large for the data
            # channel, and only the focused sharer's are kept.
            if event_name == _VIDEO_FRAME_EVENT and message is not None:
                frame = screenshare_frame(message)
                if frame is not None:
                    screenshare.handle_frame(
                        participant_id=frame[0],
                        participant_name=frame[1],
                        png_b64=frame[2],
                    )

            # Sharers are tracked as well as relayed: focus order is what tells
            # the store whose frames to keep when several people present.
            if event_name == EVENT_SCREENSHARE_OFF and message is not None:
                screenshare.screenshare_off(participant_id_of(message))
            elif event_name == EVENT_SCREENSHARE_ON and message is not None:
                screenshare.screenshare_on(participant_id_of(message))

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


def frame_shape(raw: str) -> dict[str, list[str]]:
    """Key names at the two nesting levels that have actually mattered.

    Recall wraps the payload twice: ``data`` carries artifact references
    (bot/recording/endpoint) and ``data.data`` carries the event itself. Every
    integration bug here so far has been reading one of those levels wrong, and
    a payload one level off reads as "no events arrived" everywhere downstream
    -- indistinguishable from a dead transport. Logging both once per event
    type is what makes the shape observable instead of inferred.
    """
    try:
        message = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(message, dict):
        return {}
    data = message.get("data")
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    return {
        "data_keys": sorted(data),
        "inner_keys": sorted(inner) if isinstance(inner, dict) else [],
    }


class ClassifiedEvent(NamedTuple):
    """One frame, sorted into what the relay should do with it.

    ``payload`` is set only for events that go onto the data channel. ``message``
    is the parsed frame whatever the verdict, so a consumer that handles an event
    some other way -- video frames go to GCS, not to LiveKit -- does not parse
    the largest payloads on the socket a second time.
    """

    payload: dict[str, Any] | None
    name: str
    skip_reason: str
    message: dict[str, Any] | None = None


def _classify_event(raw: str) -> ClassifiedEvent:
    """Sort one frame into relay / handle-elsewhere / skip.

    Reports the event name even when skipping, so the caller can tally what
    Recall actually sent rather than only what was forwarded. A malformed or
    unrecognised frame is skipped rather than raised: Recall reconnects on a
    closed socket, so tearing the relay down over one bad frame drops the
    events either side of it too.
    """

    try:
        message = json.loads(raw)
    except ValueError:
        return ClassifiedEvent(None, "<bad_json>", "bad_json")
    if not isinstance(message, dict):
        return ClassifiedEvent(None, "<not_an_object>", "not_an_object")

    event = message.get("event")
    if not isinstance(event, str) or not event:
        return ClassifiedEvent(None, "<no_event_field>", "no_event_field", message)
    if event == _VIDEO_FRAME_EVENT:
        return ClassifiedEvent(None, event, "video_frame", message)
    if event not in _RELAYED_EVENTS:
        return ClassifiedEvent(None, event, "not_subscribed", message)

    data = message.get("data")
    return ClassifiedEvent(
        {"event": event, "data": data if isinstance(data, dict) else {}},
        event,
        "",
        message,
    )


def screenshare_frame(message: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return ``(participant_id, participant_name, png_b64)`` for a shared screen.

    None for a webcam frame or anything unreadable. Screenshare and webcam
    frames arrive on one event distinguished only by ``type``, so this check is
    what stops somebody's face being stored as their shared screen.
    """

    outer = message.get("data")
    inner = outer.get("data") if isinstance(outer, dict) else None
    if not isinstance(inner, dict):
        return None
    if inner.get("type") != _VIDEO_FRAME_TYPE_SCREENSHARE:
        return None
    buffer = inner.get("buffer")
    if not isinstance(buffer, str) or not buffer:
        return None
    participant = inner.get("participant")
    participant = participant if isinstance(participant, dict) else {}
    participant_id = participant.get("id")
    if participant_id is None:
        return None
    # ``id`` is an int over the websocket and a string over REST; normalised so
    # 7 and "7" are not two different presenters.
    return (
        str(participant_id),
        str(participant.get("name") or ""),
        buffer,
    )


def participant_id_of(message: dict[str, Any]) -> str:
    """The participant id on a ``screenshare_on`` / ``screenshare_off`` frame."""

    outer = message.get("data")
    inner = outer.get("data") if isinstance(outer, dict) else None
    if not isinstance(inner, dict):
        return ""
    participant = inner.get("participant")
    participant = participant if isinstance(participant, dict) else {}
    participant_id = participant.get("id")
    return "" if participant_id is None else str(participant_id)
