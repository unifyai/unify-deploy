"""Contract tests for the Recall Output Media bridge page."""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client() -> TestClient:
    from communication.meet_bridge import router

    app = FastAPI()
    app.include_router(router, prefix="/meet")
    return TestClient(app)


def _page() -> str:
    response = _client().get("/meet/bridge")
    assert response.status_code == 200
    return response.text


def test_bridge_page_is_served_without_authentication() -> None:
    """A Recall bot loads this from their cloud holding no credential of ours.

    Any auth dependency here means the bot renders an error page and joins the
    meeting deaf and mute.
    """
    response = _client().get("/meet/bridge")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_bridge_page_carries_no_secret() -> None:
    """The capability is the LiveKit token in the query string, nothing baked in.

    The page is served to anyone who asks, so a credential embedded here would
    be world-readable.
    """
    page = _page().lower()
    for marker in ("api_key", "api_secret", "authorization", "bearer "):
        assert marker not in page


def test_bridge_page_disables_browser_audio_processing() -> None:
    """Meeting audio must reach the fast brain unprocessed.

    Browser DSP assumes one speaker on a headset. This feed is an already-mixed
    room, where auto gain pumps and noise suppression clips speech onsets --
    both of which surface downstream as transcription errors rather than as an
    obvious audio fault, so guard the constraint here.
    """
    page = _page()
    for constraint in (
        "echoCancellation: false",
        "noiseSuppression: false",
        "autoGainControl: false",
    ):
        assert constraint in page


def test_bridge_page_pins_the_livekit_client_version() -> None:
    """Both browser clients we ship should speak the same tested build.

    An unpinned CDN range would let a LiveKit release change the audio path of
    live customer meetings with no deploy of ours.
    """
    from communication.meet_bridge import _LIVEKIT_CLIENT_CDN

    assert "@2.15.13/" in _LIVEKIT_CLIENT_CDN
    assert _LIVEKIT_CLIENT_CDN in _page()


def test_bridge_page_is_not_cached() -> None:
    """Bots fetch the page per join, so a fix must reach the next join.

    A cached copy would strand bots on a stale bridge for the CDN/proxy TTL.
    """
    response = _client().get("/meet/bridge")
    assert response.headers["cache-control"] == "no-store"
