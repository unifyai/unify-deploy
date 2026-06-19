"""Shared LiveKit utilities for adapters and communication services."""

import json
import os
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlencode

from common.settings import SETTINGS

from livekit.api import (
    CreateAgentDispatchRequest,
    CreateSIPDispatchRuleRequest,
    EncodedFileOutput,
    GCPUpload,
    LiveKitAPI,
    RoomCompositeEgressRequest,
    SIPDispatchRuleInfo,
    TokenVerifier,
    WebhookConfig,
    WebhookReceiver,
)
from livekit.protocol.sip import (
    ListSIPDispatchRuleRequest,
    ListSIPInboundTrunkRequest,
    SIPDispatchRule,
    SIPDispatchRuleDirect,
)


def make_room_name(assistant_id: str, medium: str) -> str:
    """Canonical LiveKit room name for a given assistant and medium.

    Format: droid_{assistant_id}_{medium}
    Examples: droid_25_phone, droid_25_meet, droid_25_teams
    """
    return f"droid_{assistant_id}_{medium}"


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


async def start_room_egress(
    room_name: str,
    assistant_id: str,
    user_id: str = "",
    *,
    call_session_id: str = "",
    provider_call_sid: str = "",
    conference_name: str = "",
):
    """Start an audio-only Room Composite Egress on an existing room.

    Use this when the room was created externally (e.g. by a SIP trunk)
    and you just need to start recording.
    """
    livekit_api = get_livekit_api()
    try:
        await _start_room_egress(
            livekit_api,
            room_name,
            assistant_id,
            user_id,
            call_session_id=call_session_id,
            provider_call_sid=provider_call_sid,
            conference_name=conference_name,
        )
    except Exception as e:
        print(f"[Egress] Failed to start egress for room '{room_name}': {e}")
    finally:
        await livekit_api.aclose()


async def _start_room_egress(
    livekit_api: LiveKitAPI,
    room_name: str,
    assistant_id: str,
    user_id: str,
    *,
    call_session_id: str = "",
    provider_call_sid: str = "",
    conference_name: str = "",
):
    """Start an audio-only Room Composite Egress that writes MP3 to GCS."""
    gcs_credentials = os.getenv("GCP_SA_KEY", "")
    gcs_bucket = os.getenv("LIVEKIT_EGRESS_GCS_BUCKET", "droid-call-recordings")
    adapters_url = os.getenv("DROID_ADAPTERS_URL", "")
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    prefix = SETTINGS.deploy_env
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    filepath = f"{prefix}/{assistant_id}/{room_name}_{timestamp}.mp3"

    webhook_url = (
        f"{adapters_url}/livekit/recording-complete"
        f"?assistant_id={quote_plus(str(assistant_id))}"
        f"&user_id={quote_plus(user_id)}"
        f"&room_name={quote_plus(room_name)}"
    )
    if call_session_id:
        webhook_url += f"&call_session_id={quote_plus(call_session_id)}"
    if provider_call_sid:
        webhook_url += f"&provider_call_sid={quote_plus(provider_call_sid)}"
    if conference_name:
        webhook_url += f"&conference_name={quote_plus(conference_name)}"

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


def make_sip_uri(phone_number: str) -> str:
    """SIP URI for bridging a Twilio call into LiveKit.

    Uses the E.164 phone number as the user part so that LiveKit can match
    it against the inbound SIP trunk's ``numbers`` field.  A per-trunk
    dispatch rule (created by ``ensure_phone_dispatch_rule``) then routes
    the SIP participant into the correct ``droid_{id}_{medium}`` room.
    """
    sip_domain = os.getenv("LIVEKIT_SIP_URI", "")
    normalized = phone_number if phone_number.startswith("+") else f"+{phone_number}"
    return f"sip:{normalized}@{sip_domain}"


def make_call_scoped_sip_uri(
    phone_number: str,
    call_id: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return a unique SIP URI target for one provider call.

    Twilio can pass X-* SIP headers by appending query parameters to the SIP
    URI. The unique user part lets a per-call LiveKit dispatch rule match only
    this SIP leg instead of mutating the shared number-level rule.
    """
    sip_domain = os.getenv("LIVEKIT_SIP_URI", "")
    normalized = phone_number if phone_number.startswith("+") else f"+{phone_number}"
    safe_call_id = "".join(ch if ch.isalnum() else "-" for ch in call_id).strip("-")
    sip_user = f"{normalized[1:]}-{safe_call_id}"
    uri = f"sip:{sip_user}@{sip_domain}"
    if headers:
        sip_headers = {
            key if key.lower().startswith("x-") else f"X-{key}": value
            for key, value in headers.items()
        }
        uri = f"{uri}?{urlencode(sip_headers)}"
    return uri, sip_user


async def ensure_phone_dispatch_rule(
    phone_number: str,
    room_name: str,
) -> None:
    """Ensure a ``dispatch_rule_direct`` exists that routes SIP calls for
    *phone_number* into *room_name*.

    Idempotent: skips creation when a matching rule already exists.  When
    the room_name has changed (number reassigned to a different assistant),
    the stale rule is deleted and a fresh one is created.
    """
    livekit_api = get_livekit_api()
    try:
        normalized = (
            phone_number if phone_number.startswith("+") else f"+{phone_number}"
        )

        trunks = await livekit_api.sip.list_sip_inbound_trunk(
            ListSIPInboundTrunkRequest(),
        )
        trunk_id = None
        for t in trunks.items:
            if normalized in list(t.numbers):
                trunk_id = t.sip_trunk_id
                break
        if trunk_id is None:
            print(
                f"[SIP] No inbound trunk for {normalized}, "
                "skipping dispatch rule creation",
            )
            return

        rules = await livekit_api.sip.list_sip_dispatch_rule(
            ListSIPDispatchRuleRequest(),
        )
        for r in rules.items:
            if trunk_id not in list(r.trunk_ids):
                continue
            if (
                r.rule.HasField("dispatch_rule_direct")
                and r.rule.dispatch_rule_direct.room_name == room_name
            ):
                return
            # Stale rule for this trunk (room_name changed) — delete it.
            await livekit_api.sip.delete_sip_dispatch_rule(
                r.sip_dispatch_rule_id,
            )

        await livekit_api.sip.create_sip_dispatch_rule(
            CreateSIPDispatchRuleRequest(
                dispatch_rule=SIPDispatchRuleInfo(
                    rule=SIPDispatchRule(
                        dispatch_rule_direct=SIPDispatchRuleDirect(
                            room_name=room_name,
                        ),
                    ),
                    name=f"Droid_phone_{normalized}",
                    trunk_ids=[trunk_id],
                ),
            ),
        )
        print(f"[SIP] Created dispatch rule: {normalized} -> {room_name}")
    except Exception as e:
        print(f"[SIP] Failed to ensure dispatch rule for {phone_number}: {e}")
    finally:
        await livekit_api.aclose()


async def ensure_call_scoped_dispatch_rule(
    *,
    base_phone_number: str,
    sip_target: str,
    room_name: str,
    call_id: str,
    assistant_id: str,
) -> str | None:
    """Create a direct dispatch rule for one Twilio-created SIP leg."""
    livekit_api = get_livekit_api()
    try:
        normalized = (
            base_phone_number
            if base_phone_number.startswith("+")
            else f"+{base_phone_number}"
        )
        trunks = await livekit_api.sip.list_sip_inbound_trunk(
            ListSIPInboundTrunkRequest(),
        )
        trunk_id = None
        for trunk in trunks.items:
            if normalized in list(trunk.numbers):
                trunk_id = trunk.sip_trunk_id
                break
        if trunk_id is None:
            print(
                f"[SIP] No inbound trunk for {normalized}, "
                "skipping call-scoped dispatch rule creation",
            )
            return None

        name = f"Droid_call_{call_id}"
        dispatch = await livekit_api.sip.create_sip_dispatch_rule(
            CreateSIPDispatchRuleRequest(
                dispatch_rule=SIPDispatchRuleInfo(
                    rule=SIPDispatchRule(
                        dispatch_rule_direct=SIPDispatchRuleDirect(
                            room_name=room_name,
                        ),
                    ),
                    name=name,
                    trunk_ids=[trunk_id],
                    numbers=[sip_target],
                    attributes={
                        "call.id": call_id,
                        "assistant.id": str(assistant_id),
                    },
                ),
            ),
        )
        print(
            f"[SIP] Created call-scoped dispatch rule: " f"{sip_target} -> {room_name}",
        )
        return dispatch.sip_dispatch_rule_id
    except Exception as e:
        print(f"[SIP] Failed to ensure call-scoped dispatch rule for {call_id}: {e}")
        return None
    finally:
        await livekit_api.aclose()


async def delete_sip_dispatch_rule(dispatch_rule_id: str | None) -> None:
    """Delete a LiveKit SIP dispatch rule if it exists."""
    if not dispatch_rule_id:
        return
    livekit_api = get_livekit_api()
    try:
        await livekit_api.sip.delete_sip_dispatch_rule(dispatch_rule_id)
        print(f"[SIP] Deleted dispatch rule {dispatch_rule_id}")
    except Exception as e:
        print(f"[SIP] Failed to delete dispatch rule {dispatch_rule_id}: {e}")
    finally:
        await livekit_api.aclose()


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
