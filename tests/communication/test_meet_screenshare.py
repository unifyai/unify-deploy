"""Contract tests for the shared-screen side of the Recall relay."""

import asyncio
import base64
import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.settings import SETTINGS

RELAY_SECRET = "TEST-RELAY-SECRET"
BUCKET = "test-meet-screenshare"
# Pinned rather than inherited: object paths are prefixed by the environment, so
# a runner with DEPLOY_ENV set would otherwise change what the assertions expect.
DEPLOY_ENV = "staging"


@pytest.fixture(autouse=True)
def _relay_config():
    previous_secret = SETTINGS.recall_relay_secret
    previous_bucket = SETTINGS.meet_screenshare_bucket
    previous_env = SETTINGS.deploy_env
    SETTINGS.recall_relay_secret = RELAY_SECRET
    SETTINGS.meet_screenshare_bucket = BUCKET
    SETTINGS.deploy_env = DEPLOY_ENV
    yield
    SETTINGS.recall_relay_secret = previous_secret
    SETTINGS.meet_screenshare_bucket = previous_bucket
    SETTINGS.deploy_env = previous_env


def _client() -> TestClient:
    from communication.meet_screenshare import router

    app = FastAPI()
    app.include_router(router, prefix="/meet")
    return TestClient(app)


def _png_b64(colour: str = "red") -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 36), colour).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _video_frame(
    *,
    participant_id: int,
    name: str,
    frame_type: str = "screenshare",
) -> dict:
    """One ``video_separate_png.data`` frame in the shape Recall sends.

    Both nesting levels matter: the frame body is at ``data.data``, and a reader
    one level short sees no ``type`` at all -- which would make every webcam
    frame look like a shared screen.
    """
    return {
        "event": "video_separate_png.data",
        "data": {
            "data": {
                "buffer": _png_b64(),
                "type": frame_type,
                "participant": {"id": participant_id, "name": name},
                "timestamp": {"absolute": "2026-07-30T10:00:00Z", "relative": 1.0},
            },
            "recording": {"id": "rec-1", "metadata": {}},
            "bot": {"id": "bot-1", "metadata": {}},
        },
    }


# -------- Frame classification -------- #


def test_webcam_frames_are_not_treated_as_a_shared_screen() -> None:
    """Screenshare and webcam frames arrive on one event, split only by ``type``.

    Without the check, every participant's face is stored and described as the
    screen they are sharing.
    """
    from communication.meet_events import screenshare_frame

    webcam = _video_frame(participant_id=7, name="Ada", frame_type="webcam")
    assert screenshare_frame(webcam) is None


def test_screenshare_frames_carry_a_normalised_participant_id() -> None:
    """``id`` is an int here and a string over REST; 7 and "7" must be one person."""
    from communication.meet_events import screenshare_frame

    found = screenshare_frame(_video_frame(participant_id=7, name="Ada"))
    assert found is not None
    participant_id, name, buffer = found
    assert participant_id == "7"
    assert name == "Ada"
    assert buffer


def test_unusable_frames_are_skipped_rather_than_raised() -> None:
    """Recall reconnects on a closed socket, so one bad frame must not kill it."""
    from communication.meet_events import screenshare_frame

    assert screenshare_frame({"event": "video_separate_png.data"}) is None
    assert screenshare_frame({"data": {"data": {"type": "screenshare"}}}) is None
    assert (
        screenshare_frame(
            {"data": {"data": {"type": "screenshare", "buffer": "x"}}},
        )
        is None
    )


# -------- Focus between several presenters -------- #


@pytest.mark.asyncio
async def test_only_the_focused_presenters_frames_are_stored() -> None:
    """Two people sharing at once must not interleave into one slot."""
    from communication.meet_screenshare import ScreenshareRelay

    relay = ScreenshareRelay(room="unity_25_gmeet")
    relay.screenshare_on("7")
    relay.screenshare_on("9")

    with patch("communication.meet_screenshare._write_frame") as write:
        relay.handle_frame(
            participant_id="7",
            participant_name="Ada",
            png_b64=_png_b64(),
        )
        await asyncio.sleep(0)
        assert write.call_count == 0, "a background presenter's frame was stored"

        relay.handle_frame(
            participant_id="9",
            participant_name="Grace",
            png_b64=_png_b64(),
        )
        await asyncio.sleep(0.05)
        assert write.call_count == 1
        assert write.call_args.args[1:3] == ("9", "Grace")


@pytest.mark.asyncio
async def test_focus_falls_back_when_the_newest_presenter_stops() -> None:
    """Otherwise the slot goes dead while somebody is still sharing."""
    from communication.meet_screenshare import ScreenshareRelay

    relay = ScreenshareRelay(room="unity_25_gmeet")
    relay.screenshare_on("7")
    relay.screenshare_on("9")
    assert relay.focused == "9"

    relay.screenshare_off("9")
    assert relay.focused == "7"

    with patch("communication.meet_screenshare._write_frame") as write:
        relay.handle_frame(
            participant_id="7",
            participant_name="Ada",
            png_b64=_png_b64(),
        )
        await asyncio.sleep(0.05)
        assert write.call_count == 1


@pytest.mark.asyncio
async def test_a_frame_registers_a_presenter_we_never_saw_start() -> None:
    """A missed ``screenshare_on`` must cost ordering, not every frame."""
    from communication.meet_screenshare import ScreenshareRelay

    relay = ScreenshareRelay(room="unity_25_gmeet")
    with patch("communication.meet_screenshare._write_frame") as write:
        relay.handle_frame(
            participant_id="7",
            participant_name="Ada",
            png_b64=_png_b64(),
        )
        await asyncio.sleep(0.05)
        assert write.call_count == 1
        assert relay.focused == "7"


@pytest.mark.asyncio
async def test_frames_are_throttled_below_the_arrival_rate() -> None:
    """Recall sends 2fps; storing every one buys nothing and costs a write each."""
    from communication.meet_screenshare import ScreenshareRelay

    relay = ScreenshareRelay(room="unity_25_gmeet")
    with patch("communication.meet_screenshare._write_frame") as write:
        for _ in range(5):
            relay.handle_frame(
                participant_id="7",
                participant_name="Ada",
                png_b64=_png_b64(),
            )
            await asyncio.sleep(0.01)
        assert write.call_count == 1


# -------- Transcode + store -------- #


def test_stored_frames_are_jpeg_and_carry_who_shared_them() -> None:
    """Attribution travels with the bytes; a second object could disagree."""
    from communication.meet_screenshare import _write_frame

    blob = MagicMock()
    bucket = MagicMock()
    bucket.blob.return_value = blob
    with patch("communication.meet_screenshare._bucket", return_value=bucket):
        _write_frame("unity_25_gmeet", "7", "Ada", _png_b64())

    assert bucket.blob.call_args.args[0] == "staging/unity_25_gmeet/focus.jpg"
    assert blob.metadata == {"participant_id": "7", "participant_name": "Ada"}
    data, kwargs = blob.upload_from_string.call_args.args[0], (
        blob.upload_from_string.call_args.kwargs
    )
    assert kwargs["content_type"] == "image/jpeg"
    # JPEG start-of-image marker: the PNG really was transcoded.
    assert data[:2] == b"\xff\xd8"


def test_the_environment_prefix_separates_the_two_deployments() -> None:
    """One bucket, so the prefix is the only thing keeping the environments apart.

    Room names are derived from the assistant id and the two environments have
    independent Orchestra databases, so the same id -- and the same room name --
    can exist in both. Unprefixed, they would be the same object.
    """
    from communication.meet_screenshare import _blob_name

    SETTINGS.deploy_env = "staging"
    staging = _blob_name("unity_25_gmeet")
    SETTINGS.deploy_env = "production"
    production = _blob_name("unity_25_gmeet")

    assert staging == "staging/unity_25_gmeet/focus.jpg"
    assert production == "production/unity_25_gmeet/focus.jpg"
    assert staging != production


def test_reads_and_writes_agree_on_the_path() -> None:
    """Writer and reader both run in this service, so they cannot disagree.

    Pinned because a prefix applied on write but not on read would 404 forever
    while looking exactly like nobody sharing.
    """
    from communication.meet_screenshare import _blob_name

    blob = MagicMock()
    blob.updated = datetime.now(timezone.utc)
    blob.metadata = {}
    blob.download_as_bytes.return_value = b"\xff\xd8x"
    bucket = MagicMock()
    bucket.blob.return_value = blob
    bucket.get_blob.return_value = blob

    with patch("communication.meet_screenshare._bucket", return_value=bucket):
        from communication.meet_screenshare import _write_frame

        _write_frame("unity_25_gmeet", "7", "Ada", _png_b64())
        written = bucket.blob.call_args.args[0]

        _client().get(
            f"/meet/screenshare/unity_25_gmeet/focus.jpg?token={RELAY_SECRET}",
        )
        read = bucket.get_blob.call_args.args[0]

    assert written == read == _blob_name("unity_25_gmeet")


def test_a_store_failure_does_not_propagate() -> None:
    """This runs beside the event relay carrying speaker attribution and chat."""
    from communication.meet_screenshare import _write_frame

    with patch(
        "communication.meet_screenshare._bucket",
        side_effect=RuntimeError("no such bucket"),
    ):
        _write_frame("unity_25_gmeet", "7", "Ada", _png_b64())


# -------- Serving the frame to the assistant pod -------- #


def _stored_blob(*, age_s: float) -> MagicMock:
    blob = MagicMock()
    blob.updated = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    blob.metadata = {"participant_id": "7", "participant_name": "Ada"}
    blob.download_as_bytes.return_value = b"\xff\xd8jpeg-bytes"
    return blob


def test_focus_endpoint_rejects_a_wrong_token() -> None:
    """The pod's only credential here is the same shared secret Recall uses."""
    resp = _client().get("/meet/screenshare/unity_25_gmeet/focus.jpg?token=nope")
    assert resp.status_code == 403


def test_focus_endpoint_serves_the_frame_with_its_attribution() -> None:
    bucket = MagicMock()
    bucket.get_blob.return_value = _stored_blob(age_s=1.0)
    with patch("communication.meet_screenshare._bucket", return_value=bucket):
        resp = _client().get(
            f"/meet/screenshare/unity_25_gmeet/focus.jpg?token={RELAY_SECRET}",
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-screenshare-participant-name"] == "Ada"
    assert resp.headers["x-screenshare-participant-id"] == "7"
    assert resp.content == b"\xff\xd8jpeg-bytes"


def test_a_stale_frame_is_not_served() -> None:
    """Room names are derived from the assistant id and so outlive any meeting.

    Without an age bound, ``unity_25_gmeet`` serves the screen somebody shared in
    a different call days ago as though it were up now.
    """
    bucket = MagicMock()
    bucket.get_blob.return_value = _stored_blob(age_s=600.0)
    with patch("communication.meet_screenshare._bucket", return_value=bucket):
        resp = _client().get(
            f"/meet/screenshare/unity_25_gmeet/focus.jpg?token={RELAY_SECRET}",
        )

    assert resp.status_code == 404


def test_no_frame_reads_as_nobody_sharing() -> None:
    bucket = MagicMock()
    bucket.get_blob.return_value = None
    with patch("communication.meet_screenshare._bucket", return_value=bucket):
        resp = _client().get(
            f"/meet/screenshare/unity_25_gmeet/focus.jpg?token={RELAY_SECRET}",
        )

    assert resp.status_code == 404


def test_an_unprovisioned_bucket_reads_as_nobody_sharing() -> None:
    """A new environment must join meetings, just without shared screens."""
    SETTINGS.meet_screenshare_bucket = ""
    resp = _client().get(
        f"/meet/screenshare/unity_25_gmeet/focus.jpg?token={RELAY_SECRET}",
    )
    assert resp.status_code == 404


# -------- Relay wiring -------- #


def test_video_frames_never_reach_the_data_channel() -> None:
    """A 360p JPEG is orders of magnitude over a reliable data message."""
    from communication.meet_events import _classify_event

    classified = _classify_event(json.dumps(_video_frame(participant_id=7, name="Ada")))
    assert classified.payload is None
    assert classified.skip_reason == "video_frame"
    # Parsed once, not twice: the frame is the largest payload on the socket.
    assert classified.message is not None
