"""Media bridge page rendered inside a Recall.ai Output Media bot.

A Recall bot joins Google Meet / Teams and renders this page as its camera or
screenshare. Recall wires the page to the meeting in both directions:

* meeting audio arrives as the page's **microphone** (Recall auto-grants the
  permission, so ``getUserMedia`` resolves with no user gesture), and
* whatever the page **plays** is captured and sent into the meeting.

So the bridge is an ordinary LiveKit browser client: publish the "microphone"
into the assistant's room, and play back the assistant's track. The fast brain
then hears and speaks over the same LiveKit transport that already carries
phone, whatsapp_call and unify_meet, with no virtual audio devices in between.

The page holds no secrets and needs no auth of its own. It is inert until
loaded with a ``token`` query parameter -- a short-TTL, room-scoped LiveKit
grant minted by the assistant pod, which is the only capability involved.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# Pinned to the version console already ships (console/package.json), so both
# browser clients speak to LiveKit through the same tested client build.
_LIVEKIT_CLIENT_CDN = (
    "https://cdn.jsdelivr.net/npm/livekit-client@2.15.13/dist/livekit-client.umd.min.js"
)

_BRIDGE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Unify meeting bridge</title>
<style>
  html, body {
    margin: 0;
    height: 100%;
    background: #0b0d10;
    color: #e8eaed;
    font: 500 28px/1.4 ui-sans-serif, system-ui, -apple-system, sans-serif;
  }
  main {
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 20px;
    text-align: center;
    padding: 48px;
    box-sizing: border-box;
  }
  #label { font-size: 44px; font-weight: 600; }
  #state { font-size: 24px; color: #9aa4b2; }
  #state.error { color: #ff8a80; }
  .dot {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: #9aa4b2;
    display: inline-block;
    margin-right: 10px;
    vertical-align: middle;
  }
  .dot.live { background: #4ade80; }
  .dot.error { background: #ff8a80; }
</style>
</head>
<body>
<main>
  <div id="label"></div>
  <div id="state"><span class="dot" id="dot"></span><span id="stateText">starting</span></div>
</main>
<script src="__LIVEKIT_CDN__"></script>
<script>
(async () => {
  const params = new URLSearchParams(window.location.search);
  const serverUrl = params.get("url");
  const token = params.get("token");
  const labelEl = document.getElementById("label");
  const stateTextEl = document.getElementById("stateText");
  const dotEl = document.getElementById("dot");

  labelEl.textContent = params.get("label") || "Unify";

  const setState = (text, kind) => {
    stateTextEl.textContent = text;
    dotEl.className = "dot" + (kind ? " " + kind : "");
    document.getElementById("state").className = kind === "error" ? "error" : "";
  };

  if (!serverUrl || !token) {
    setState("missing url or token", "error");
    return;
  }

  const { Room, RoomEvent, createLocalAudioTrack } = window.LivekitClient;
  // Reconnect rather than die: a bot can outlive a transient LiveKit blip, and
  // a dead bridge is a bot sitting silently in a real meeting.
  const room = new Room({ reconnectPolicy: { maxRetries: 30 } });

  // Remote audio has to be *played* for Recall to capture it into the meeting.
  const sink = document.createElement("div");
  document.body.appendChild(sink);

  room.on(RoomEvent.TrackSubscribed, (track) => {
    if (track.kind !== "audio") return;
    const el = track.attach();
    el.autoplay = true;
    sink.appendChild(el);
  });
  room.on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((el) => el.remove()));
  room.on(RoomEvent.Disconnected, () => setState("disconnected", "error"));
  room.on(RoomEvent.Reconnecting, () => setState("reconnecting"));
  room.on(RoomEvent.Reconnected, () => setState("live", "live"));

  try {
    await room.connect(serverUrl, token);

    // Browser DSP is tuned for a single speaker at a headset. This feed is an
    // already-mixed room of several people, where AGC pumps and noise
    // suppression eats speech onsets -- both of which land as transcription
    // errors downstream. Recall performs no processing of its own, so the
    // meeting audio must be published exactly as it arrives.
    const track = await createLocalAudioTrack({
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    });
    await room.localParticipant.publishTrack(track);

    // Autoplay policy blocks playback in a normal browser without a gesture.
    // The bot browser allows it, but a silent failure here is indistinguishable
    // from a mute assistant, so surface it on the page instead.
    if (!room.canPlaybackAudio) {
      try {
        await room.startAudio();
      } catch (err) {
        setState("audio playback blocked", "error");
        return;
      }
    }

    setState("live", "live");
  } catch (err) {
    setState("connect failed: " + (err && err.message ? err.message : err), "error");
  }
})();
</script>
</body>
</html>
"""


@router.get("/bridge", include_in_schema=False)
async def meet_bridge_page() -> HTMLResponse:
    """Serve the Output Media bridge page for a Recall bot to render."""

    return HTMLResponse(
        content=_BRIDGE_PAGE.replace("__LIVEKIT_CDN__", _LIVEKIT_CLIENT_CDN),
        headers={"cache-control": "no-store"},
    )
