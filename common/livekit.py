"""Shared LiveKit utilities for adapters and communication services.

Call recording is deliberately absent here. Egress lives in exactly one
place -- ``unify.gateway.common.livekit`` -- reached over
``/phone/start-recording``. A second copy in this module drifted out of sync
with the gateway during the phone-channel migration and silently stopped
recording every dispatched call for months, so the duplication is not
reintroduced: adapters observe recordings (via the completion webhook) but
never start them.
"""

import json
import os
from urllib.parse import urlencode

from livekit.api import (
    CreateAgentDispatchRequest,
    CreateSIPDispatchRuleRequest,
    LiveKitAPI,
    SIPDispatchRuleInfo,
    TokenVerifier,
    WebhookReceiver,
)
from livekit.protocol.sip import (
    DeleteSIPDispatchRuleRequest,
    ListSIPDispatchRuleRequest,
    ListSIPInboundTrunkRequest,
    SIPDispatchRule,
    SIPDispatchRuleDirect,
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
):
    """Create a LiveKit room and dispatch an agent into it."""
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
        return dispatch
    except Exception as e:
        print(f"Error creating room and dispatching LiveKit agent: {str(e)}")
        raise
    finally:
        await livekit_api.aclose()


def make_sip_uri(phone_number: str) -> str:
    """SIP URI for bridging a Twilio call into LiveKit.

    Uses the E.164 phone number as the user part so that LiveKit can match
    it against the inbound SIP trunk's ``numbers`` field.  A per-trunk
    dispatch rule (created by ``ensure_phone_dispatch_rule``) then routes
    the SIP participant into the correct ``unity_{id}_{medium}`` room.
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
                DeleteSIPDispatchRuleRequest(
                    sip_dispatch_rule_id=r.sip_dispatch_rule_id,
                ),
            )

        await livekit_api.sip.create_sip_dispatch_rule(
            CreateSIPDispatchRuleRequest(
                dispatch_rule=SIPDispatchRuleInfo(
                    rule=SIPDispatchRule(
                        dispatch_rule_direct=SIPDispatchRuleDirect(
                            room_name=room_name,
                        ),
                    ),
                    name=f"Unity_phone_{normalized}",
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

        name = f"Unity_call_{call_id}"
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
        await livekit_api.sip.delete_sip_dispatch_rule(
            DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=dispatch_rule_id),
        )
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
