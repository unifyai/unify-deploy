"""The assistant's desktop, rendered for a Recall bot to screenshare.

A Recall bot can only put a *webpage* on its screenshare surface -- there is no
API to push video into it -- so sharing the assistant's desktop means hosting a
page that shows the desktop. This is that page.

It does not reach into the VM. The assistant pod publishes its desktop into the
call's LiveKit room as a video track and this page subscribes to it, so the
desktop is never publicly reachable and no credential is handed to Recall beyond
a room-scoped, subscribe-only token in the URL.

Two properties are load-bearing and easy to break:

* **Silent.** Recall captures the page's audio *and* video. The bridge page
  (``meet_bridge.py``) already plays the assistant's voice into the meeting; if
  this page played it too, the meeting would hear the assistant twice. Audio is
  never subscribed here, let alone attached.
* **Its own LiveKit identity.** LiveKit evicts a duplicate identity, so joining
  under the bridge's name would kick the page carrying audio and leave the
  assistant silent for the rest of the call.

Not to be confused with ``meet_screenshare.py``, which is the opposite
direction: frames of a *participant's* shared screen, on their way to the brain.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# Pinned to the version the bridge page uses, so both browser clients speak to
# LiveKit through the same tested build.
_LIVEKIT_CLIENT_CDN = (
    "https://cdn.jsdelivr.net/npm/livekit-client@2.15.13/dist/livekit-client.umd.min.js"
)

_DESKTOP_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Unify desktop</title>
<style>
  /* Recall renders this page at a fixed 1280x720 and captures the result, so the
     layout is built for exactly that viewport rather than being responsive. */
  html, body {
    margin: 0;
    height: 100%;
    background: #000;
    color: #e8eaed;
    font: 500 24px/1.4 ui-sans-serif, system-ui, -apple-system, sans-serif;
    overflow: hidden;
  }
  #stage {
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  video {
    max-width: 100%;
    max-height: 100%;
    /* contain, never cover: a cropped desktop hides whatever sat at the edge. */
    object-fit: contain;
    background: #000;
  }
  #placeholder {
    text-align: center;
    color: #9aa4b2;
  }
  #placeholder.hidden { display: none; }
</style>
</head>
<body>
<main id="stage">
  <div id="placeholder"><span id="placeholderText">Connecting…</span></div>
</main>
<script src="__LIVEKIT_CDN__"></script>
<script>
(async () => {
  const params = new URLSearchParams(window.location.search);
  const serverUrl = params.get("url");
  const token = params.get("token");
  const stage = document.getElementById("stage");
  const placeholder = document.getElementById("placeholder");
  const placeholderText = document.getElementById("placeholderText");

  const setPlaceholder = (text) => {
    placeholderText.textContent = text;
    placeholder.classList.remove("hidden");
  };

  if (!serverUrl || !token) {
    setPlaceholder("missing url or token");
    return;
  }

  const { Room, RoomEvent, Track } = window.LivekitClient;
  // Reconnect rather than die: this surface is live in a real meeting, and a
  // dead page is a black rectangle somebody is waiting on.
  const room = new Room({ reconnectPolicy: { maxRetries: 30 } });

  let videoEl = null;

  const showVideo = (track) => {
    if (videoEl) {
      videoEl.remove();
    }
    videoEl = track.attach();
    videoEl.autoplay = true;
    // Muted is belt-and-braces: this is a video track, but an element that
    // could ever play audio would be captured into the meeting.
    videoEl.muted = true;
    placeholder.classList.add("hidden");
    stage.appendChild(videoEl);
  };

  // Subscribe to video only, by opting out of automatic subscription entirely.
  // Ignoring audio at attach time would be enough to keep it silent, but not
  // subscribing at all also spares the bandwidth and removes any route
  // for it to reach an element by accident.
  const subscribeIfVideo = (publication) => {
    if (publication.kind === Track.Kind.Video) {
      publication.setSubscribed(true);
    }
  };

  room.on(RoomEvent.TrackPublished, (publication) => subscribeIfVideo(publication));
  room.on(RoomEvent.TrackSubscribed, (track) => {
    if (track.kind !== Track.Kind.Video) return;
    showVideo(track);
  });
  room.on(RoomEvent.TrackUnsubscribed, (track) => {
    if (track.kind !== Track.Kind.Video) return;
    track.detach().forEach((el) => el.remove());
    videoEl = null;
    setPlaceholder("Waiting for the desktop…");
  });
  room.on(RoomEvent.Disconnected, () => setPlaceholder("Disconnected"));
  room.on(RoomEvent.Reconnecting, () => setPlaceholder("Reconnecting…"));

  try {
    await room.connect(serverUrl, token, { autoSubscribe: false });
    setPlaceholder("Waiting for the desktop…");
    // Anything already published before this page loaded. The pod starts
    // publishing before the bot is pointed here, so this is the normal path
    // rather than a race guard.
    room.remoteParticipants.forEach((participant) => {
      participant.trackPublications.forEach(subscribeIfVideo);
    });
  } catch (err) {
    setPlaceholder("Connect failed: " + (err && err.message ? err.message : err));
  }
})();
</script>
</body>
</html>
"""


@router.get("/desktop", include_in_schema=False)
async def meet_desktop_page() -> HTMLResponse:
    """Serve the desktop page for a Recall bot to render as its screenshare."""

    return HTMLResponse(
        content=_DESKTOP_PAGE.replace("__LIVEKIT_CDN__", _LIVEKIT_CLIENT_CDN),
        headers={"cache-control": "no-store"},
    )
