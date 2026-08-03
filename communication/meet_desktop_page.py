"""The assistant's managed desktop, framed for a Recall bot to screenshare.

A Recall bot can only put a *webpage* on its screenshare surface -- there is no
API to push video into it -- so sharing the assistant's desktop means hosting a
page that shows the desktop. This is that page.

It frames the managed VM's own noVNC liveview, the same surface Console's Desktop
pane embeds, so the meeting sees the real desktop live rather than a
reconstruction of it. The wrapper is deliberately thin, because the liveview is
cross-origin and that bounds what it can do:

* it **can** give the liveview a viewport exactly the size Recall captures, and
  cover it with a transparent layer so stray input cannot reach the desktop;
* it **cannot** restyle or hide anything inside the frame, so whatever chrome the
  liveview draws is what the meeting sees, and only the liveview's own query
  parameters can change that;
* it **cannot** reliably detect a failed cross-origin load, so the fallback here
  is a message painted *behind* the frame rather than an error state it claims to
  have observed.

Not to be confused with ``meet_screenshare.py``, which is the opposite
direction: frames of a *participant's* shared screen, on their way to the brain.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import secrets
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from common.settings import SETTINGS

router = APIRouter()


def sign_liveview(liveview: str, password: str, secret: str) -> str:
    """Signature proving a liveview URL came from an assistant pod.

    The route is unauthenticated, because a Recall bot loads it from their cloud
    with no credential of ours. Without a signature it would happily frame any
    URL anyone handed it, on our own domain -- a ready-made clickjacking surface.
    Signing binds the page to URLs our own pods minted.

    Mirrors ``brain``-side minting in unify's ``recall/provider.py``; the shared
    secret is the same ``RECALL_RELAY_SECRET`` the event relay and frame store
    already use.
    """

    payload = f"{liveview}\x00{password}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


# What Recall renders and captures. Fixed, not configurable, so the liveview is
# handed exactly this and scales itself into it.
_CAPTURE_WIDTH = 1280
_CAPTURE_HEIGHT = 720

_DESKTOP_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Unify desktop</title>
<style>
  html, body {
    margin: 0;
    height: 100%;
    background: #000;
    color: #9aa4b2;
    font: 500 22px/1.4 ui-sans-serif, system-ui, -apple-system, sans-serif;
    overflow: hidden;
  }
  #fallback {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 48px;
  }
  /* Above the fallback, so the message only shows through if the frame paints
     nothing. Sized to the capture viewport rather than stretched: the liveview
     scales itself, and stretching it here would letterbox twice. */
  iframe {
    position: absolute;
    inset: 0;
    width: __WIDTH__px;
    height: __HEIGHT__px;
    border: 0;
    background: #000;
  }
  /* The bot's browser forwards no input, but the desktop is live and this costs
     one element. */
  #shield {
    position: absolute;
    inset: 0;
    background: transparent;
  }
</style>
</head>
<body>
  <div id="fallback">Waiting for the desktop…</div>
  <iframe
    src="__LIVEVIEW__"
    title="Assistant desktop"
    referrerpolicy="no-referrer"
    allow="clipboard-read; clipboard-write"
  ></iframe>
  <div id="shield"></div>
</body>
</html>
"""


@router.get("/desktop", include_in_schema=False)
async def meet_desktop_page(
    liveview: str = Query(...),
    password: str = Query(""),
    sig: str = Query(""),
) -> HTMLResponse:
    """Frame one managed desktop's liveview at the bot's capture size."""

    expected = SETTINGS.recall_relay_secret
    if not expected or not sig:
        return HTMLResponse(content="unsigned", status_code=403)
    if not secrets.compare_digest(sig, sign_liveview(liveview, password, expected)):
        return HTMLResponse(content="bad signature", status_code=403)

    parsed = urlparse(liveview)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return HTMLResponse(content="invalid liveview", status_code=400)

    separator = "&" if parsed.query else "?"
    target = f"{liveview}{separator}{urlencode({'password': password})}"

    page = (
        # Escaped because it lands in an HTML attribute: a quote in the URL would
        # otherwise close ``src`` and let the rest be read as markup.
        _DESKTOP_PAGE.replace("__LIVEVIEW__", html.escape(target, quote=True))
        .replace("__WIDTH__", str(_CAPTURE_WIDTH))
        .replace("__HEIGHT__", str(_CAPTURE_HEIGHT))
    )
    return HTMLResponse(content=page, headers={"cache-control": "no-store"})
