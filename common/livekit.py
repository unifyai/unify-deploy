"""Shared LiveKit utilities for adapters and communication services."""

import json
import os
from datetime import datetime, timezone
from urllib.parse import quote_plus

from livekit.api import (
    CreateAgentDispatchRequest,
    EncodedFileOutput,
    GCPUpload,
    LiveKitAPI,
    RoomCompositeEgressRequest,
    TokenVerifier,
    WebhookConfig,
    WebhookReceiver,
)


def make_room_name(assistant_id: str, medium: str) -> str:
    """Canonical LiveKit room name for a given assistant and medium.

    Format: unity_{assistant_id}_{medium}
    Examples: unity_25_phone, unity_25_meet, unity_25_teams
    """
    return f"unity_{assistant_id}_{medium}"


def get_livekit_api() -> LiveKitAPI:
    """Get a LiveKit API client from environment variables."""
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not url or not api_key or not api_secret:
        raise RuntimeError(
            "LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET must be set",
        )

    return LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)


async def create_room_and_dispatch_agent(
    room_name: str,
    agent_name: str,
    metadata: dict = None,
    *,
    record: bool = False,
    assistant_id: str = "",
    user_id: str = "",
):
    """Create a LiveKit room, dispatch an agent, and optionally start recording.

    When *record* is True, a Room Composite Egress (audio-only, MP3) is
    started for the room. The recording is uploaded to GCS by LiveKit and a
    completion webhook notifies this service so it can publish a
    recording_ready Pub/Sub event.
    """
    livekit_api = get_livekit_api()

    try:
        dispatch_request = CreateAgentDispatchRequest(
            agent_name=agent_name,
            room=room_name,
            metadata=json.dumps(metadata) if metadata else None,
        )
        dispatch = await livekit_api.agent_dispatch.create_dispatch(dispatch_request)
        print(
            f"Successfully created room '{room_name}' and dispatched "
            f"LiveKit agent '{agent_name}'",
        )
        print(f"Dispatch ID: {dispatch.id}")

        if record:
            await _start_room_egress(livekit_api, room_name, assistant_id, user_id)

        return dispatch
    except Exception as e:
        print(f"Error creating room and dispatching LiveKit agent: {str(e)}")
        raise
    finally:
        await livekit_api.aclose()


async def start_room_egress(room_name: str, assistant_id: str, user_id: str = ""):
    """Start an audio-only Room Composite Egress on an existing room.

    Use this when the room was created externally (e.g. by a SIP trunk)
    and you just need to start recording.
    """
    livekit_api = get_livekit_api()
    try:
        await _start_room_egress(livekit_api, room_name, assistant_id, user_id)
    except Exception as e:
        print(f"[Egress] Failed to start egress for room '{room_name}': {e}")
    finally:
        await livekit_api.aclose()


async def _start_room_egress(
    livekit_api: LiveKitAPI,
    room_name: str,
    assistant_id: str,
    user_id: str,
):
    """Start an audio-only Room Composite Egress that writes MP3 to GCS."""
    gcs_credentials = os.getenv("GCP_SA_KEY", "")
    gcs_bucket = os.getenv("LIVEKIT_EGRESS_GCS_BUCKET", "unity-call-recordings")
    adapters_url = os.getenv("UNITY_ADAPTERS_URL", "")
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    is_staging = bool(os.getenv("STAGING"))

    prefix = "staging" if is_staging else "production"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    filepath = f"{prefix}/{assistant_id}/{room_name}_{timestamp}.mp3"

    webhook_url = (
        f"{adapters_url}/livekit/recording-complete"
        f"?assistant_id={quote_plus(str(assistant_id))}"
        f"&user_id={quote_plus(user_id)}"
        f"&room_name={quote_plus(room_name)}"
    )

    egress_request = RoomCompositeEgressRequest(
        room_name=room_name,
        audio_only=True,
        file_outputs=[
            EncodedFileOutput(
                file_type=3,  # MP3
                filepath=filepath,
                gcp=GCPUpload(
                    credentials=gcs_credentials,
                    bucket=gcs_bucket,
                ),
            ),
        ],
        webhooks=[
            WebhookConfig(url=webhook_url, signing_key=api_key),
        ],
    )
    info = await livekit_api.egress.start_room_composite_egress(egress_request)
    print(
        f"[Egress] Started room composite egress {info.egress_id} "
        f"for room '{room_name}' -> gs://{gcs_bucket}/{filepath}",
    )


def verify_livekit_webhook(body: str, auth_token: str):
    """Verify a LiveKit webhook signature and return the parsed event.

    Raises on verification failure.
    """
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    api_secret = os.getenv("LIVEKIT_API_SECRET", "")
    receiver = WebhookReceiver(
        TokenVerifier(api_key=api_key, api_secret=api_secret),
    )
    return receiver.receive(body, auth_token)
