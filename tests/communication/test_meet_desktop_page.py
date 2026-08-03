"""Contract tests for the page a Recall bot renders as the assistant's screenshare.

The page is a string of HTML, so most of these are text assertions. The ones that
matter are about what it refuses: the route is unauthenticated -- a Recall bot
loads it from their cloud with no credential of ours -- so anything it will frame
for anyone is a clickjacking surface on our own domain.
"""

import hashlib
import hmac
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.settings import SETTINGS

RELAY_SECRET = "TEST-RELAY-SECRET"
LIVEVIEW = "https://vm.example.com/desktop/custom.html"
PASSWORD = "vnc-pass"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _relay_secret():
    previous = SETTINGS.recall_relay_secret
    SETTINGS.recall_relay_secret = RELAY_SECRET
    yield
    SETTINGS.recall_relay_secret = previous


def _client() -> TestClient:
    from communication.meet_desktop_page import router

    app = FastAPI()
    app.include_router(router, prefix="/meet")
    return TestClient(app)


def _sig(liveview: str = LIVEVIEW, password: str = PASSWORD) -> str:
    payload = f"{liveview}\x00{password}".encode()
    return hmac.new(RELAY_SECRET.encode(), payload, hashlib.sha256).hexdigest()


def _get(**params):
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return _client().get(f"/meet/desktop?{query}")


def test_a_signed_liveview_is_framed() -> None:
    resp = _get(liveview=LIVEVIEW, password=PASSWORD, sig=_sig())

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert LIVEVIEW in resp.text
    assert "<iframe" in resp.text


def test_an_unsigned_request_is_refused() -> None:
    """Otherwise this frames anything, for anyone, on our domain."""
    assert _get(liveview=LIVEVIEW, password=PASSWORD).status_code == 403


def test_a_forged_signature_is_refused() -> None:
    assert _get(liveview=LIVEVIEW, password=PASSWORD, sig="deadbeef").status_code == 403


def test_a_signature_for_a_different_liveview_is_refused() -> None:
    """The signature has to cover the URL, or the URL could just be swapped."""
    resp = _get(
        liveview="https://evil.example.com/page.html",
        password=PASSWORD,
        sig=_sig(),
    )
    assert resp.status_code == 403


def test_the_route_fails_closed_with_no_secret_configured() -> None:
    """An unset secret must reject every caller, not accept every caller."""
    SETTINGS.recall_relay_secret = ""
    assert _get(liveview=LIVEVIEW, password=PASSWORD, sig=_sig()).status_code == 403


def test_a_non_http_liveview_is_refused() -> None:
    """Keeps the page from being pointed at javascript: or data: URLs."""
    target = "javascript:alert(1)"
    resp = _get(liveview=target, password="", sig=_sig(target, ""))
    assert resp.status_code == 400


def test_the_liveview_cannot_break_out_of_the_src_attribute() -> None:
    """It lands in an HTML attribute, so a quote would end it and start markup."""
    target = 'https://vm.example.com/x.html?a="><script>alert(1)</script>'
    resp = _get(liveview=target, password="", sig=_sig(target, ""))

    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text or "&quot;" in resp.text


def test_the_frame_is_sized_to_what_recall_captures() -> None:
    """Recall renders at a fixed 1280x720; the liveview scales itself into it."""
    page = _get(liveview=LIVEVIEW, password=PASSWORD, sig=_sig()).text
    assert "1280px" in page
    assert "720px" in page


def test_input_cannot_reach_the_desktop() -> None:
    """The desktop is live. The bot forwards no input, but this costs one div."""
    assert 'id="shield"' in _get(liveview=LIVEVIEW, password=PASSWORD, sig=_sig()).text


def test_the_signature_matches_the_minting_side() -> None:
    """Two repos construct this; a mismatch is a 403 read as "could not share"."""
    from communication.meet_desktop_page import sign_liveview

    assert sign_liveview(LIVEVIEW, PASSWORD, RELAY_SECRET) == _sig()
