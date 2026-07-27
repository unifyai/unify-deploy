# Call Recording Architecture

This document is the ground truth for how voice calls and Unify Meets are recorded, stored, and linked to transcript data. All design decisions and implementation details are captured here. Every future decision regarding call recording should be based on this document.

## Table of Contents

- [Design Principles](#design-principles)
- [High-Level Flow](#high-level-flow)
- [Recording Mechanism: LiveKit Egress](#recording-mechanism-livekit-egress)
- [GCS Storage Layout](#gcs-storage-layout)
- [Pub/Sub Event: recording\_ready](#pubsub-event-recording_ready)
- [Transcript Integration](#transcript-integration)
- [Per-Utterance Timing](#per-utterance-timing)
- [End-to-End Walkthrough: Phone Call](#end-to-end-walkthrough-phone-call)
- [End-to-End Walkthrough: Unify Meet](#end-to-end-walkthrough-unify-meet)
- [Repos and Files Involved](#repos-and-files-involved)
- [Environment Variables](#environment-variables)
- [Infrastructure Prerequisites](#infrastructure-prerequisites)
- [Current Limitations and Known Gaps](#current-limitations-and-known-gaps)

---

## Design Principles

1. **One recording mechanism, one implementation.** Every LiveKit-native call type (phone/PSTN, WhatsApp calls, Unify Meets) is recorded via LiveKit Room Composite Egress. There is no Twilio-specific recording path, and the egress request is built in exactly one place: `unify/gateway/common/livekit.py`. A second copy of this logic previously lived in `unity-deploy/common/livekit.py`; it drifted out of sync during the phone-channel migration and silently disabled recording for every dispatched call, so the duplication must not be reintroduced.

2. **Egress starts when the room is live — never at dispatch.** Room Composite Egress needs a publishing participant. Started at SIP-bridge setup or agent dispatch it races the join, and LiveKit aborts the job with `Start signal not received` after producing **no file at all**. Recording is therefore requested from the runtime's call-started path (`PhoneCallStarted` / `WhatsAppCallStarted` / `UnifyMeetStarted`) via `POST /phone/start-recording`.

3. **Not every channel can be recorded this way.** Browser meets (Google Meet, Teams) bridge caller audio through the agent-service PortAudio device; **that audio never enters the LiveKit room**, so the compositor has nothing to mix. Rooms ending in `_gmeet` / `_teams` are refused by `start_room_egress`. Recording those channels requires a capture point on the bridge and is not implemented.

4. **Starting a recording is idempotent.** `start_room_egress` checks for an already-running egress on the room and no-ops if one exists, so a retried or duplicated call-started event cannot record a room twice into two files.

5. **Recording is a property of an exchange, not a separate entity.** A call or Unify Meet maps to a single transcript exchange. The recording URL lives on that exchange's metadata, making it self-contained. There is no separate recordings table or registry.

6. **LiveKit writes directly to GCS.** LiveKit Egress uploads the recording file to a GCS bucket. No service downloads or re-uploads the file.

7. **Asynchronous linking, with a persistent fallback.** The recording URL is not available at call-end time; it arrives later via a LiveKit webhook. The session identifiers are stored on the exchange at call-end time and cached in-process. Because egress finalises minutes after the room closes — often after the pod has been recycled — the `RecordingReady` handler falls back to a server-side lookup on the identifiers persisted in exchange metadata.

8. **Exchange metadata merges, never replaces.** Multiple independent writers contribute to one exchange (identifiers at call end, recording URL later). A replacing write would drop the identifiers needed to resolve the exchange in the first place.

9. **Only a completed egress with real bytes is published.** A failed or aborted egress still emits `egress_ended` and can still name a file that was never written. The completion webhook checks `status == EGRESS_COMPLETE` and a non-zero size before publishing `recording_ready`, so a dead URL is never attached to a transcript.

10. **One recording per session.** Each call or meet produces a single MP3 file covering the full duration of the session. There is no chunk-by-chunk recording.

---

## High-Level Flow

```
┌──────────────┐     ┌──────────────────┐     ┌─────────────┐     ┌──────────────┐
│ Call starts   │────►│  Communication   │────►│ LiveKit     │────►│ GCS Bucket   │
│ (Twilio or   │     │  starts egress   │     │ Egress      │     │ (MP3 file)   │
│  WebRTC)     │     │  on LiveKit room │     │ records     │     │              │
└──────────────┘     └──────────────────┘     │ full call   │     └──────┬───────┘
                                               └──────┬──────┘            │
                                                      │                   │
                                                      │  egress complete  │
                                                      ▼                   │
                                               ┌──────────────────────┐   │
                                               │  Adapter             │   │
                                               │  /livekit/           │   │
                                               │  recording-complete  │   │
                                               │  (ensures job alive, │   │
                                               │   publishes Pub/Sub) │   │
                                               └──────┬───────────────┘   │
                                                      │                   │
                                                      │ Pub/Sub           │
                                                      │ recording_ready   │
                                                      ▼                   │
                                               ┌──────────────────┐       │
                                               │  Unity receives  │◄──────┘
                                               │  RecordingReady  │  (URL references
                                               │  event           │   the GCS file)
                                               └──────┬───────────┘
                                                      │
                                                      │ stores recording_url
                                                      │ on exchange metadata
                                                      ▼
                                               ┌──────────────────┐
                                               │  Exchange now    │
                                               │  has recording   │
                                               │  URL inline      │
                                               └──────────────────┘
```

---

## Recording Mechanism: LiveKit Egress

All recording uses **LiveKit Room Composite Egress** — a server-side feature where LiveKit mixes all audio tracks in a room and writes the result to a file.

### Why LiveKit Egress (not Twilio Conference Recording)

Phone calls flow through both Twilio and LiveKit: the PSTN side is handled by a Twilio Conference, which bridges via SIP to a LiveKit room where the AI agent lives. LiveKit Egress is used rather than Twilio's built-in conference recording for three reasons:

- **Unified path.** Unify Meets only go through LiveKit (no Twilio). Using LiveKit Egress for both means one recording mechanism instead of two.
- **Direct GCS upload.** LiveKit Egress writes directly to GCS with zero intermediate hops. Twilio recording would require downloading from Twilio, uploading to GCS, and notifying a backend — three hops.
- **No dependency on Twilio for recording.** The Twilio Conference is used for PSTN bridging, but recording is decoupled from it.

### Egress Configuration

The egress is started via `start_room_egress()` in `unify/gateway/common/livekit.py` — the single implementation:

- **Format:** MP3 (audio-only, `file_type=3`)
- **Mixing:** Room Composite — all audio tracks in the room are mixed into a single file
- **Upload target:** GCS bucket via `GCPUpload` with credentials from `GCP_SA_KEY`
- **Webhook:** On completion, LiveKit calls `{UNITY_ADAPTERS_URL}/livekit/recording-complete?assistant_id=X&room_name=Y`, signed with `LIVEKIT_API_KEY`

`start_room_egress` refuses to start in three cases, each of which would otherwise produce a broken or duplicate recording:

| Refusal | Why |
|---|---|
| Room ends in `_gmeet` / `_teams` | Channel audio never reaches the LiveKit room, so the job can only abort with no file |
| `assistant_id` is empty | The object path is `{env}/{assistant_id}/{room}.mp3`; an empty id collapses the prefix and strands the file |
| An egress is already active on the room | Two starters would record the room twice, into two separate billed files |

### When Egress Starts

Recording is requested **once the session is live**, from the runtime, for every LiveKit-native channel:

| Call Type | Where Recording is Requested | Code Location |
|-----------|------------------------------|---------------|
| **Phone call** (Twilio, inbound + outbound) | `PhoneCallStarted` handler | `unify/conversation_manager/domains/event_handlers.py` → `_start_session_recording` |
| **WhatsApp call** | `WhatsAppCallStarted` handler | same |
| **Unify Meet** | `UnifyMeetStarted` handler | same |
| **Google Meet / Teams Meet** | Not recorded — see Design Principle 3 | — |

The handler calls `start_call_recording()` (`unify/conversation_manager/utils.py`), which POSTs to `{COMMS_URL}/phone/start-recording` on the gateway. Neither the adapters' Twilio webhooks nor `/dispatch-livekit-agent` start egress.

Phone/WhatsApp/browser-meet room names are produced by `make_room_name(assistant_id, medium)` which returns `unity_{assistant_id}_{medium}` (e.g. `unity_25_phone`, `unity_25_teams`); those channels use the same value as both the LiveKit room name and the agent name. Unify Meets are call sessions: their rooms are named `unity_call_{session_id}` by Orchestra, one room per session, and each assistant registers a distinct per-assistant agent name (`unity_{assistant_id}`).

### Egress Lifecycle

1. **Start**: `start_room_egress()` calls LiveKit's `start_room_composite_egress` API
2. **Recording**: LiveKit Egress runs server-side, recording all audio in the room for the full duration
3. **End**: When the room closes (all participants leave), egress finalizes the file
4. **Upload**: LiveKit writes the MP3 to the configured GCS bucket
5. **Webhook**: LiveKit sends a POST to the adapter's `/livekit/recording-complete` endpoint

---

## GCS Storage Layout

**Bucket:** `unity-call-recordings` (configurable via `LIVEKIT_EGRESS_GCS_BUCKET`)

**File path pattern:** `{environment}/{assistant_id}/{room_name}_{timestamp}.mp3`

The timestamp is UTC in `YYYY-MM-DDTHH-MM-SS` format, generated at egress start time. This ensures each recording gets a unique filename even if the same room name is reused across sessions.

| Environment | Prefix | Example Path |
|-------------|--------|--------------|
| Staging | `staging/` | `staging/25/unity_25_phone_2026-02-19T16-30-45.mp3` |
| Production | `production/` | `production/25/unity_25_phone_2026-02-19T16-30-45.mp3` |

The environment is determined by the `STAGING` env var in the Communication service. If `STAGING` is truthy, the prefix is `staging`; otherwise `production`.

**Public URL format:** `https://storage.googleapis.com/unity-call-recordings/{prefix}/{assistant_id}/{room_name}_{timestamp}.mp3`

This is the URL stored in exchange metadata as `recording_url`. The Console's `AudioPlayer` component can take this URL and generate a signed URL for playback via the Console's own `/api/media/get` route.

---

## Pub/Sub Event: recording_ready

When LiveKit Egress completes and the webhook fires, the **adapter** (not the comms app) handles the webhook, ensures the assistant's Unity container is running, and publishes a Pub/Sub message to the assistant's topic.

### Message Format

```json
{
    "thread": "recording_ready",
    "event": {
        "assistant_id": "25",
        "conference_name": "unity_25_phone",
        "recording_url": "https://storage.googleapis.com/unity-call-recordings/staging/25/unity_25_phone_2026-02-19T16-30-45.mp3"
    }
}
```

**Key fields:**
- `conference_name` — the LiveKit room name. This is the join key used to match the recording to its exchange. For phone calls: `unity_25_phone`; for Unify Meets: the session room `unity_call_{session_id}`.
- `recording_url` — the public GCS URL for the MP3 file.

### Publishing Logic

The `/livekit/recording-complete` adapter endpoint in `adapters/main.py`:
1. Verifies the LiveKit webhook signature via `verify_livekit_webhook()` (shared helper in `common/livekit.py`)
2. Calls `build_webhook_context()` with `validate_contact=False, ensure_job=True` to ensure the assistant's Unity container is alive (starts a new job if the container shut down while waiting for the recording)
3. Constructs the Pub/Sub topic name: `unity-{assistant_id}[-staging]`
4. Publishes the `recording_ready` JSON message to that topic

---

## Transcript Integration

Recording data is linked to transcripts via the **Exchange** abstraction. Each call/meet session maps to exactly one exchange, and the recording metadata lives on that exchange.

### Data Model

Exchanges support arbitrary `metadata` (a dict). The recording-related keys are:

| Key | Set When | Set By | Value |
|-----|----------|--------|-------|
| `conference_name` | Phone call ends (`PhoneCallEnded`) | Event handler in `event_handlers.py` | Twilio conference name, e.g. `Unity_12025551234_2026_02_18_10_30_00` |
| `room_name` | Unify Meet ends (`UnifyMeetEnded`) | Event handler in `event_handlers.py` | LiveKit room name, e.g. `unity_call_{session_id}` |
| `recording_url` | Recording is ready (`RecordingReady`) | Event handler in `event_handlers.py` | Full GCS public URL |

### How Exchange Metadata is Populated

**Step 1: At call/meet end** — The `PhoneCallEnded`/`UnifyMeetEnded` handler:
- Reads the `exchange_id` from `call_manager.call_exchange_id` or `call_manager.unify_meet_exchange_id`
- Reads the session identifier from `call_manager.conference_name` or `call_manager.room_name`
- Calls `transcript_manager.update_exchange_metadata(exchange_id, {"conference_name": ...})` (or `{"room_name": ...}`)
- Stashes the mapping `session_identifier -> exchange_id` in `cm._recording_exchange_ids` (an in-memory dict on the ConversationManager)

**Step 2: When recording arrives** — The `RecordingReady` handler:
- Pops the `exchange_id` from `cm._recording_exchange_ids` using the `conference_name` from the event
- Calls `transcript_manager.update_exchange_metadata(exchange_id, {"recording_url": ...})`

This two-step approach is necessary because the recording arrives asynchronously — often seconds or minutes after the call ends. The in-memory dict bridges the gap without requiring a database query.

### Exchange ID Assignment

Each call/meet session gets a single exchange ID, assigned when the first message (utterance) is logged:
- `call_exchange_id` for phone calls
- `unify_meet_exchange_id` for Unify Meets

These are set in the `LogMessageResponse` handler via `log_first_message_in_new_exchange()` and reset to `UNASSIGNED` (-1) at call end.

### Result

After both steps complete, the exchange for a call looks like:

```python
{
    "exchange_id": 42,
    "medium": "phone_call",  # or "unify_meet"
    "metadata": {
        "conference_name": "Unity_12025551234_2026_02_18_10_30_00",  # or "room_name" for meets
        "recording_url": "https://storage.googleapis.com/unity-call-recordings/staging/25/unity_25_phone_2026-02-19T16-30-45.mp3"
    },
    "messages": [
        {"content": "Hello?", "sender_id": 1, "metadata": {"call_utterance_timestamp": "00.03"}},
        {"content": "Hi, how can I help?", "sender_id": 0, "metadata": {"call_utterance_timestamp": "00.07"}},
        ...
    ]
}
```

---

## Per-Utterance Timing

Each message logged during a call/meet includes a `call_utterance_timestamp` in its metadata. This is a timestamp offset from the start of the call, formatted as `MM.SS`.

### How It Works

In `managers_utils.py`, every time a call utterance is logged:
1. The call start time is read from `call_manager.call_start_timestamp` (phone) or `call_manager.unify_meet_start_timestamp` (meet)
2. The delta from call start to now is computed
3. For assistant utterances, 2 seconds are added to approximate TTS playback delay
4. The result is formatted as `MM.SS` (e.g. `"02.15"` = 2 minutes 15 seconds into the call)
5. This is stored in `message.metadata["call_utterance_timestamp"]`

### Timestamps Are Set at Call Start

The `PhoneCallStarted`/`UnifyMeetStarted` handler sets:
- `cm.call_manager.call_start_timestamp = event.timestamp` (for phone calls)
- `cm.call_manager.unify_meet_start_timestamp = event.timestamp` (for meets)

These are cleared at call end.

### Purpose

Per-utterance timestamps enable precise time-alignment of transcript text to audio playback. A consumer can take the `recording_url` from exchange metadata and the `call_utterance_timestamp` from each message to highlight which utterance is playing at any given point in the audio.

---

## End-to-End Walkthrough: Phone Call

1. **Incoming call** → Twilio webhook hits `adapters/main.py` `/twilio/call`
2. **Conference setup** → `create_conference_response()` creates a Twilio Conference (no recording flag — recording is handled by LiveKit)
3. **SIP bridge** → Twilio bridges the PSTN caller to a LiveKit room via SIP trunk. Room name: `unity_{assistant_id}_phone` (from `make_room_name()`). **No egress is started here** — the room has no publishing participant yet.
4. **Pub/Sub** → Adapter publishes `call` thread event to `unity-{assistant_id}[-staging]` topic
5. **Unity receives call** → `CommsManager` routes to `PhoneCallReceived` event → sets `conference_name` on `call_manager`
6. **Call starts** → `PhoneCallStarted` event → sets `call_start_timestamp` on `call_manager`
7. **Start recording** → the same handler calls `start_call_recording()` → `POST /phone/start-recording` → `start_room_egress()`. The room is live, so the compositor attaches immediately. Fire-and-forget: a recording failure never disturbs the call.
8. **Utterances** → Each utterance is logged with `call_utterance_timestamp` in message metadata
9. **LiveKit Egress records** → Server-side, LiveKit mixes all audio tracks in the room into a single MP3, streaming to GCS
10. **Call ends** → `PhoneCallEnded` event handler:
    - Merges `conference_name`, `room_name`, `call_session_id`, `provider_call_sid` into exchange metadata
    - Stashes each identifier → `exchange_id` in `_recording_exchange_ids`
    - Clears all session state (timestamps, exchange IDs, conference_name)
11. **Egress completes** → LiveKit finishes writing MP3 to GCS, fires webhook to `/livekit/recording-complete` on the adapters
12. **Adapter handler** → Verifies LiveKit signature; **drops the event unless the egress completed with a non-empty file**; ensures the Unity container is alive (starts a job if needed), constructs `recording_url`, publishes `recording_ready` Pub/Sub event
13. **Unity receives recording** → `CommsManager` routes to `RecordingReady` event → the handler resolves `exchange_id` from `_recording_exchange_ids`, falling back to a server-side lookup on the identifiers stored in step 10 when the original container is gone, then merges `recording_url` into exchange metadata

---

## End-to-End Walkthrough: Unify Meet

1. **User starts meet** → Orchestra creates the call session and names the room `unity_call_{session_id}`; the adapters' `/unify_meet` webhook publishes the roster
2. **Pub/Sub** → A `unify_meet` thread event is published to `unity-{assistant_id}[-staging]` topic
3. **Unity receives meet** → `CommsManager` routes to `UnifyMeetReceived` event → `call_manager.start_unify_meet()` sets `room_name`, then dispatches the voice agent (prewarmed worker, else a subprocess). Dispatch does **not** start recording.
4. **Meet starts** → `UnifyMeetStarted` event → sets `unify_meet_start_timestamp` on `call_manager`
5. **Start recording** → same `_start_session_recording` path as a phone call, carrying `call_session_id` as the linkage ID. A meet that is never answered produces no room and therefore no egress attempt.
6. **Utterances** → Each utterance is logged with `call_utterance_timestamp` in message metadata
7. **LiveKit Egress records** → Same as phone calls — full room audio mixed to MP3
8. **Meet ends** → `UnifyMeetEnded` event handler merges `room_name` + `call_session_id` into exchange metadata and stashes the mapping
9. **Egress completes** → Same adapter webhook flow as phone calls (`/livekit/recording-complete`)
10. **Unity receives recording** → Same `RecordingReady` handler as phone calls

---

## Why Google Meet and Teams Meet Are Not Recorded

A browser meet runs a headless browser on the agent-service and bridges audio through a PortAudio device. The assistant's own speech is published into the LiveKit room, but **remote participant audio is never published there** — `Assistant.stt_node` in `medium_scripts/call.py` feeds the recogniser from `audio_bridge.capture_q` precisely because the LiveKit room carries no caller audio on these channels.

Room Composite Egress therefore has nothing meaningful to mix. Requesting it produces a compositor that waits for a track that never arrives, stays alive for as long as the room does, and finally aborts with `Start signal not received` and no file — while consuming an egress slot the whole time. `start_room_egress` refuses `_gmeet` / `_teams` rooms for this reason.

Recording these channels requires capturing at the bridge (or using the provider's own recording) and is deliberately out of scope.

---

## Self-Host Divergence

The self-hosted single-process stack does **not** use the flow above. It keeps its own egress helper in `unify/conversation_manager/local_providers/livekit.py`, started from `local_ingress.py`, because its completion webhook must land on `/local/livekit/recording-complete` — a route only the local ingress serves. The gateway always points completion webhooks at `UNITY_ADAPTERS_URL`, which in self-host resolves to the gateway itself (no such route), so routing self-host recording through `/phone/start-recording` would upload a file whose completion event nobody receives.

`_start_session_recording` therefore returns early when `LOCAL_COMMS_ENABLED` is set (or `LOCAL_COMMS_MODE == "local"`), leaving the local ingress in charge.

Consequence: the self-host path still starts egress at Twilio-webhook time and so retains the early-start weakness this guide describes. It is a dev-only path; unifying it means giving the local ingress a call-started trigger of its own.

---

## Repos and Files Involved

### unify-deploy (adapters, `unity-deploy/`)

| File | Role |
|------|------|
| `common/livekit.py` | Shared LiveKit utilities: `make_room_name()`, `get_livekit_api()`, `create_room_and_dispatch_agent()`, `verify_livekit_webhook()`. **No egress logic** — see Design Principle 1 |
| `adapters/main.py` | `/livekit/recording-complete` webhook handler: verifies signature, drops non-completed / empty egress, ensures the job is alive, back-links the call session, publishes `recording_ready`. The Twilio call + WhatsApp call webhooks do **not** start egress |

### unify (runtime + gateway, `unify/`)

| File | Role |
|------|------|
| `unify/gateway/common/livekit.py` | **The** egress implementation: `start_room_egress()`, `room_supports_egress()`, `has_active_egress()`, plus `create_room_and_dispatch_agent()` (dispatch only) |
| `unify/gateway/channels/phone/views.py` | `/phone/start-recording` (starts egress) and `/phone/dispatch-livekit-agent` (dispatch only) |
| `unify/conversation_manager/utils.py` | `start_call_recording()` and `dispatch_livekit_agent()` — the runtime's two calls into the gateway |
| `unify/conversation_manager/events.py` | `RecordingReady` event dataclass |
| `unify/conversation_manager/comms_manager.py` | Routes the `recording_ready` Pub/Sub thread to the `RecordingReady` event |
| `unify/conversation_manager/domains/event_handlers.py` | `_start_session_recording()` (call-started → request recording), `RecordingReady` handler (resolves the exchange, merges the URL), `*CallEnded`/`*MeetEnded` handler (persists session identifiers) |
| `unify/conversation_manager/domains/call_manager.py` | `make_room_name()`, `conference_name` / `room_name` / `call_session_id` / `provider_call_sid` attributes, `start_call()`, `start_unify_meet()` |
| `unify/conversation_manager/domains/managers_utils.py` | `call_utterance_timestamp` computation and storage in message metadata |
| `unify/conversation_manager/conversation_manager.py` | `_recording_exchange_ids: dict[str, int]` in-memory mapping |
| `unify/transcript_manager/transcript_manager.py` | `update_exchange_metadata()` (merging), `resolve_exchange_id_by_metadata()` (persistent fallback lookup) |

### Orchestra (`orchestra/`)

Orchestra does not participate in producing a recording. It does hold two relevant pieces of state: `communication_call_sessions.recording_url` (back-linked by the completion webhook when a `provider_call_sid` is known) and `/v0/storage/signed-url`, which mints playback URLs for the `unity-call-recordings` bucket.

### Console (`console/`)

No recording playback surface exists yet. `CallPill.recordingUrl` is declared but never populated, and the `TranscriptsPane` does not read the `Exchanges` table, so `recording_url` never reaches the UI. The `AudioPlayer` component (used by Interfaces blocks) can render a GCS URL via `/api/media/get`. Wiring playback into the transcripts pane is pending.

---

## Environment Variables

### Required for recording to work

| Variable | Service | Purpose |
|----------|---------|---------|
| `GCP_SA_KEY` | comms app (gateway) | GCS service account **JSON content** (not a path) that LiveKit Egress uses to upload recordings to the bucket |
| `LIVEKIT_API_KEY` | comms app + adapters | LiveKit API key — used to start egress and verify webhook signatures |
| `LIVEKIT_API_SECRET` | comms app + adapters | LiveKit API secret — used alongside `LIVEKIT_API_KEY` |
| `LIVEKIT_URL` | comms app + adapters | LiveKit server URL |
| `UNITY_ADAPTERS_URL` | comms app | Public URL of the adapters service — the base for the egress completion webhook |
| `UNITY_COMMS_URL` | assistant runtime | Public URL of the comms gateway — where `start_call_recording()` POSTs |
| `GCP_PROJECT_ID` | adapters | GCP project ID — used for Pub/Sub topic path construction |
| `DEPLOY_ENV` | comms app | Object prefix: `staging` / `production` / `preview` |

Note that `GCP_SA_KEY` is only needed by the service that *starts* egress — the comms gateway. The adapters need the LiveKit keys (to verify the completion webhook) but not the GCS credentials.

### Optional

| Variable | Service | Default | Purpose |
|----------|---------|---------|---------|
| `LIVEKIT_EGRESS_GCS_BUCKET` | comms app | `unity-call-recordings` | GCS bucket name for recordings |

---

## Infrastructure Prerequisites

For recording to work end-to-end, the following must be true:

1. **LiveKit Egress must be enabled** on the LiveKit Cloud project (or a self-hosted Egress service must be running). Without this, `start_room_composite_egress` API calls will fail.

2. **`GCP_SA_KEY`** must be set on both the adapters and communication Cloud Run services. This is a GCS service account JSON string with write permissions on the `unity-call-recordings` bucket. This is the same key already used by the adapters for other GCS operations (message attachments, Gmail).

3. **`UNITY_ADAPTERS_URL` must be publicly reachable** from LiveKit's infrastructure, so the egress completion webhook can reach `/livekit/recording-complete` on the adapters.

4. **The `/livekit/recording-complete` adapter endpoint** has no application-level auth beyond LiveKit's own webhook signing (verified via `WebhookReceiver`/`TokenVerifier`). If there's infrastructure-level auth (API gateway, load balancer) that blocks unauthenticated requests to the adapters service, the webhook will be rejected.

5. **The GCS bucket `unity-call-recordings` must exist** in the GCP project (`<gcp-project-comms>`), with the service account from `GCP_SA_KEY` having write access.

6. **GCP_PROJECT_ID** must be set (already required for all other Pub/Sub publishing).

### Failure modes

All egress-related code is wrapped in exception handlers. If any prerequisite is missing:
- Calls and meets still work normally — the recording code is fire-and-forget
- The recording simply won't be produced or linked
- Errors are logged to stdout (e.g. `[Egress] Non-fatal: failed to start egress for call: ...`)

---

## Current Limitations and Known Gaps

1. **Browser meets are not recorded at all.** Google Meet and Teams Meet produce no recording — see "Why Google Meet and Teams Meet Are Not Recorded". Supporting them needs a capture point on the agent-service audio bridge, or the provider's own recording.

2. **No signed URL generation in the recording flow.** The `recording_url` stored on exchange metadata is a raw GCS public URL. Playback goes through Orchestra's `/v0/storage/signed-url` (or the Console's `/api/media/get`); direct API consumers must sign it themselves.

3. **No Console playback surface yet.** The recording URL reaches exchange metadata but nothing renders it. See the Console row in "Repos and Files Involved".

4. **No retry on webhook failure.** If the `/livekit/recording-complete` webhook fails (network issue, adapters service down), LiveKit may retry depending on its configuration, but there's no application-level retry or dead-letter queue. The recording file would exist in GCS but the Pub/Sub event would never be published. The persistent exchange lookup does not help here — no event is ever delivered.

5. **Only Unify Meets carry a durable session-level recording pointer for non-telephony calls.** The completion webhook back-links `recording_url` onto `communication_call_sessions` only when a `provider_call_sid` is present (phone / WhatsApp). `CallSession` (org calls) has no `recording_url` column, so for meets the exchange metadata is the only home.

6. **Recording depends on the call-started event arriving.** If `*CallStarted` / `UnifyMeetStarted` is never published (or is dropped), no recording is started even though the call proceeds. This trades a silent no-recording for the previous behaviour of a guaranteed-failing egress; the call-started path is also what drives the transcript, so its loss is already visible.
