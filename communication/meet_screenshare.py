"""Store and serve the screen a meeting participant is sharing.

Recall streams per-participant video frames over the same websocket that carries
participant events (see ``meet_events.py``). Screenshare frames are the ones
worth keeping: they are what a human is deliberately showing the room, and the
assistant is otherwise blind to them.

The frames cannot ride the LiveKit data channel the participant events use -- a
360p JPEG is a couple of orders of magnitude over what a reliable data message
should carry -- and they cannot sit in this process's memory either, because
comms runs on Cloud Run with no session affinity: the instance holding a bot's
websocket is almost never the instance an assistant pod's poll lands on. So the
frame goes to GCS, which both instances can reach, and the pod polls for it.

Only the focused sharer's frame is stored. Focus is latest-presenter-wins,
matching how the assistant already picks between several people sharing into a
LiveKit room, and it falls back to the previous sharer when the newest one
stops.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Query, Response

from common.settings import SETTINGS

logger = logging.getLogger(__name__)

router = APIRouter()

# Object metadata keys carrying who the frame belongs to. Attribution is the
# reason we take frames from Recall rather than scraping the meeting UI, so it
# travels with the bytes instead of in a second object that could disagree.
_META_PARTICIPANT_ID = "participant_id"
_META_PARTICIPANT_NAME = "participant_name"

# How long a stored frame stays servable. This is not a cache tuning knob: room
# names are derived from the assistant id (``unity_{id}_meet``) and so are reused
# by every meeting that assistant ever holds. Without an age bound, yesterday's
# shared screen is served into today's call. It also covers a share that ended
# without a ``screenshare_off`` and a relay instance that died mid-call.
_FRAME_MAX_AGE_S = 15.0

# Frames arrive at 2fps. Halving that is plenty for a shared screen -- the brain
# reads one frame per turn -- and keeps the write rate off the meeting's own
# critical path.
_MIN_WRITE_INTERVAL_S = 1.0

# JPEG rather than the PNG Recall sends: every consumer downstream already
# expects JPEG, and a screenshare re-encodes to a fraction of the size.
_JPEG_QUALITY = 85


def _blob_name(room: str) -> str:
    """Object path for one room's focused frame.

    Environment is a path prefix rather than a separate bucket, matching call
    recordings (``{deploy_env}/{assistant}/{room}_{ts}.mp3``). It also keeps
    staging and production apart where the room name alone would not: room names
    are derived from the assistant id, and the two environments have independent
    Orchestra databases, so the same id -- and therefore the same room -- can
    exist in both.

    The only place the prefix is applied, for reads as well as writes. Both sides
    run in this service off one ``DEPLOY_ENV``, so they cannot disagree about
    which environment's frames they are handling.
    """

    return f"{SETTINGS.deploy_env}/{room}/focus.jpg"


_storage_client = None


def _bucket():
    """The frames bucket, or None when this environment has none provisioned.

    The bucket is deliberately never created on demand: buckets are provisioned
    with a lifecycle rule attached, not conjured by whichever instance noticed
    first. The client is cached because this is called once per stored frame --
    credential discovery every second, for the length of every meeting, is a
    cost with nothing to show for it.
    """

    global _storage_client

    name = SETTINGS.meet_screenshare_bucket
    if not name:
        return None
    if _storage_client is None:
        import json

        from google.cloud import storage
        from google.oauth2.service_account import Credentials

        creds_json = os.getenv("GCP_SA_KEY")
        if creds_json:
            _storage_client = storage.Client(
                credentials=Credentials.from_service_account_info(
                    json.loads(creds_json),
                ),
            )
        else:
            _storage_client = storage.Client()
    return _storage_client.bucket(name)


@dataclass
class ScreenshareRelay:
    """One bot's screenshare frames, from arrival to stored object.

    Lives for the length of a websocket connection, so the focus order it keeps
    is per-meeting and needs no eviction.
    """

    room: str
    # Sharer ids in the order they started sharing; the last one is focused.
    # A frame from an unknown id registers that id, so a missed
    # ``screenshare_on`` costs attribution ordering rather than every frame.
    _sharing: list[str] = field(default_factory=list)
    _last_write_at: float = 0.0
    _write_in_flight: bool = False

    def screenshare_on(self, participant_id: str) -> None:
        if participant_id and participant_id not in self._sharing:
            self._sharing.append(participant_id)

    def screenshare_off(self, participant_id: str) -> None:
        """Drop a sharer, refocusing on whoever was sharing before them."""

        if participant_id in self._sharing:
            self._sharing.remove(participant_id)

    @property
    def focused(self) -> str | None:
        return self._sharing[-1] if self._sharing else None

    def handle_frame(
        self,
        *,
        participant_id: str,
        participant_name: str,
        png_b64: str,
    ) -> None:
        """Store one screenshare frame if it is the focused sharer's and due.

        Returns immediately; the encode and upload run off the event loop, which
        also carries this bot's participant events and must not stall behind
        GCS.
        """

        if not participant_id:
            return
        self.screenshare_on(participant_id)
        if participant_id != self.focused:
            return

        now = time.monotonic()
        if self._write_in_flight or now - self._last_write_at < _MIN_WRITE_INTERVAL_S:
            return
        self._last_write_at = now
        self._write_in_flight = True

        async def _store() -> None:
            try:
                await asyncio.to_thread(
                    _write_frame,
                    self.room,
                    participant_id,
                    participant_name,
                    png_b64,
                )
            finally:
                self._write_in_flight = False

        asyncio.create_task(_store())


def _write_frame(
    room: str,
    participant_id: str,
    participant_name: str,
    png_b64: str,
) -> None:
    """Transcode one frame to JPEG and overwrite the room's focused object.

    Failures are logged and swallowed. This runs alongside the participant-event
    relay that carries speaker attribution and inbound chat for the same call;
    an unprovisioned bucket or a transient GCS error must not cost those.
    """

    from PIL import Image

    try:
        raw = base64.b64decode(png_b64)
    except (ValueError, TypeError):
        logger.warning({"event": "meet_screenshare_bad_base64", "room": room})
        return

    try:
        buf = io.BytesIO()
        with Image.open(io.BytesIO(raw)) as img:
            img.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY)
        bucket = _bucket()
        if bucket is None:
            logger.warning({"event": "meet_screenshare_no_bucket", "room": room})
            return
        blob = bucket.blob(_blob_name(room))
        blob.metadata = {
            _META_PARTICIPANT_ID: participant_id,
            _META_PARTICIPANT_NAME: participant_name,
        }
        blob.cache_control = "no-store"
        blob.upload_from_string(buf.getvalue(), content_type="image/jpeg")
    except Exception as exc:  # noqa: BLE001 - best effort beside the event relay
        logger.warning(
            {
                "event": "meet_screenshare_store_failed",
                "room": room,
                "error": repr(exc),
            },
        )


def _authorized(token: str | None) -> bool:
    """Whether a token matches the shared relay secret.

    The same secret the bot's websocket uses. The reader is the assistant pod,
    which already holds it to build the relay URL in the first place.
    """

    expected = SETTINGS.recall_relay_secret
    if not expected or not token:
        return False
    return secrets.compare_digest(token, expected)


@router.get("/screenshare/{room}/focus.jpg", include_in_schema=False)
async def meet_screenshare_focus(
    room: str,
    token: str = Query(None),
) -> Response:
    """Serve the focused shared screen for one room, with who is sharing it."""

    if not _authorized(token):
        return Response(status_code=403)

    def _read() -> tuple[bytes, dict, float] | None:
        bucket = _bucket()
        if bucket is None:
            return None
        blob = bucket.get_blob(_blob_name(room))
        if blob is None or blob.updated is None:
            return None
        from datetime import datetime, timezone

        age = (datetime.now(timezone.utc) - blob.updated).total_seconds()
        return blob.download_as_bytes(), dict(blob.metadata or {}), age

    try:
        found = await asyncio.to_thread(_read)
    except Exception as exc:  # noqa: BLE001 - a blind assistant, not a 500
        logger.warning(
            {"event": "meet_screenshare_read_failed", "room": room, "error": repr(exc)},
        )
        return Response(status_code=404)

    if found is None:
        return Response(status_code=404)
    data, meta, age = found
    if age > _FRAME_MAX_AGE_S:
        # Stale means nobody is sharing right now, which is a 404 rather than an
        # old picture: the room name outlives the meeting.
        return Response(status_code=404)

    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "cache-control": "no-store",
            "x-screenshare-participant-id": meta.get(_META_PARTICIPANT_ID, ""),
            "x-screenshare-participant-name": meta.get(_META_PARTICIPANT_NAME, ""),
            "x-screenshare-age-ms": str(int(age * 1000)),
        },
    )
