"""Contract tests for the page a Recall bot renders as the assistant's screenshare.

The page is a string of HTML, so these are text assertions rather than behaviour.
That is worth doing anyway: the two properties that matter are both invisible
mistakes -- a page that plays audio sounds fine in a browser and doubles the
assistant's voice only once it is inside a real meeting, and a page that
subscribes to everything looks identical until it costs bandwidth it never uses.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _page() -> str:
    from communication.meet_desktop_page import router

    app = FastAPI()
    app.include_router(router, prefix="/meet")
    resp = TestClient(app).get("/meet/desktop")
    assert resp.status_code == 200
    return resp.text


def test_the_page_is_served_and_not_cached() -> None:
    from communication.meet_desktop_page import router

    app = FastAPI()
    app.include_router(router, prefix="/meet")
    resp = TestClient(app).get("/meet/desktop")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert "livekit-client" in resp.text


def test_the_page_never_plays_audio() -> None:
    """Recall captures this page's audio into the meeting.

    The bridge page already plays the assistant's voice there, so anything this
    page played would arrive twice. It must not start audio playback, and it must
    not attach a track without checking the kind first.
    """
    page = _page()
    # No playback unblocking, no audio track ever referenced, and the one media
    # element that exists is muted.
    assert "startAudio" not in page
    assert "Track.Kind.Audio" not in page
    assert "muted = true" in page


def test_the_page_subscribes_to_video_only() -> None:
    """Opting out of automatic subscription is what keeps audio off the wire."""
    page = _page()
    assert "autoSubscribe: false" in page
    assert "Track.Kind.Video" in page
    assert "setSubscribed(true)" in page


def test_the_page_shows_something_before_the_desktop_arrives() -> None:
    """The surface is live the instant Recall loads it.

    A blank rectangle in a real meeting reads as a broken share rather than as a
    share that has not started yet.
    """
    page = _page()
    assert "placeholder" in page
    assert "Waiting for the desktop" in page


def test_the_page_reconnects_rather_than_dying() -> None:
    """A dead page is a black rectangle somebody in the meeting is waiting on."""
    assert "maxRetries: 30" in _page()


def test_the_desktop_is_never_cropped() -> None:
    """``cover`` would hide whatever sat at the edge of the screen being shown."""
    page = _page()
    assert "object-fit: contain" in page
    assert "object-fit: cover" not in page
