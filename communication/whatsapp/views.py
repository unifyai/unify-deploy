import asyncio
import base64
import json
import logging
import os
import time

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request
from livekit.api import (
    LiveKitAPI,
    SIPInboundTrunkInfo,
    CreateSIPInboundTrunkRequest,
)

from communication.helpers import get_twilio_client, get_twilio_wa_client
from common.settings import SETTINGS

logger = logging.getLogger(__name__)

auth_router = APIRouter()
unauth_router = APIRouter()


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}


def _twilio_whatsapp_auth_headers() -> dict:
    account_sid = os.getenv("TWILIO_WA_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_WA_AUTH_TOKEN")
    auth_str = f"{account_sid}:{auth_token}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    return {"Authorization": f"Basic {b64_auth}", "Content-Type": "application/json"}


async def _resolve_route(assistant_id: int, contact_number: str) -> dict:
    """Get or create a route for an outbound message.

    Returns {"pool_number": str, "window_open": bool}.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SETTINGS.orchestra_url}/admin/whatsapp/route",
            json={"assistant_id": assistant_id, "contact_number": contact_number},
            headers=_admin_headers(),
            timeout=10.0,
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    data = resp.json()
    return {
        "pool_number": data["pool_number"],
        "window_open": data.get("window_open", True),
    }


# ---------------------------------------------------------------------------
# Unauthenticated endpoints (Twilio status callbacks)
# ---------------------------------------------------------------------------


@unauth_router.post("/status")
async def check_whatsapp_status(
    MessageStatus: str = Form(...),
    To: str = Form(...),
    From: str = Form(...),
    MessageSid: str = Form(None),
    callback_id: str = Query(None),
):
    logger.info(
        f"[WhatsApp Status Callback] MessageStatus: {MessageStatus}, "
        f"To: {To}, From: {From}, MessageSid: {MessageSid}, "
        f"callback_id: {callback_id}",
    )

    if callback_id:
        await _forward_notification_status(callback_id, To, MessageSid, MessageStatus)

    return {"status": True, "message_status": MessageStatus}


async def _forward_notification_status(
    callback_id: str,
    to: str,
    message_sid: str | None,
    status: str,
) -> None:
    """Forward a delivery receipt to Orchestra's notification-status endpoint."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{SETTINGS.orchestra_url}/admin/whatsapp/notification-status",
                headers=_admin_headers(),
                json={
                    "callback_id": callback_id,
                    "to": to.replace("whatsapp:", ""),
                    "message_sid": message_sid,
                    "status": status,
                },
                timeout=10.0,
            )
            if resp.status_code >= 400:
                logger.error(
                    f"Failed to forward notification status: "
                    f"{resp.status_code} {resp.text}",
                )
    except Exception:
        logger.exception("Error forwarding notification status to Orchestra")


# ---------------------------------------------------------------------------
# Authenticated endpoints (admin API)
# ---------------------------------------------------------------------------


GREETING_TEMPLATE_SID = "HX002f6aeb3b4e5a79b693fa7190196612"
NUMBER_CHANGE_TEMPLATE_SID = "HXd9c362371aefe97f10526f1c0974f7a2"
VOICE_CALL_TEMPLATE_SID = "HX885d46e6ccb82e4313ef1a42181c142d"
VOICE_CALL_REQUEST_TEMPLATE_SID = "HX67bc29b24fb597e6fad501ea68d2566e"


@auth_router.post("/notify")
async def notify(request: Request):
    """Send number-change template notifications to affected users."""
    data = await request.json()
    from_number = data["from_number"]
    recipients = data["recipients"]
    old_contact = data["old_contact"]
    new_contact = data["new_contact"]
    callback_id = data.get("callback_id")

    status_callback = f"{SETTINGS.comms_url}/whatsapp/status"
    if callback_id:
        status_callback += f"?callback_id={callback_id}"

    twilio_client = get_twilio_wa_client()
    results = {}
    for r in recipients:
        to = r["to"]
        if not to:
            continue
        msg = twilio_client.messages.create(
            content_sid=NUMBER_CHANGE_TEMPLATE_SID,
            to=f"whatsapp:{to}",
            from_=f"whatsapp:{from_number}",
            content_variables=json.dumps(
                {
                    "user_name": r["user_name"],
                    "agent_name": r["agent_name"],
                    "old_contact": old_contact,
                    "new_contact": new_contact,
                }
            ),
            status_callback=status_callback,
        )
        results[to] = {"sid": msg.sid, "status": "sent"}

    return {"results": results}


@auth_router.post("/send")
async def send(request: Request):
    data = await request.json()
    to = data["to"]
    body = data["body"]
    assistant_id = data["assistant_id"]
    user_name = data.get("user_name", "")
    agent_name = data.get("agent_name", "")

    route = await _resolve_route(assistant_id, to)
    pool_number = route["pool_number"]
    window_open = route["window_open"]

    twilio_client = get_twilio_wa_client()
    if window_open:
        twilio_client.messages.create(
            to=f"whatsapp:{to}",
            from_=f"whatsapp:{pool_number}",
            body=body,
            status_callback=f"{SETTINGS.comms_url}/whatsapp/status",
        )
        method = "freeform"
    else:
        twilio_client.messages.create(
            content_sid=GREETING_TEMPLATE_SID,
            to=f"whatsapp:{to}",
            from_=f"whatsapp:{pool_number}",
            content_variables=json.dumps(
                {
                    "user_name": user_name,
                    "agent_name": agent_name,
                },
            ),
            status_callback=f"{SETTINGS.comms_url}/whatsapp/status",
        )
        method = "template"

    return {"success": True, "method": method}


async def _check_call_permission(pool_number: str, contact_number: str) -> bool:
    """Check with Orchestra whether outbound WhatsApp calling is permitted."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SETTINGS.orchestra_url}/admin/whatsapp/call-permission",
                params={
                    "pool_number": pool_number,
                    "contact_number": contact_number,
                },
                headers=_admin_headers(),
                timeout=10.0,
            )
        if resp.status_code >= 400:
            return False
        return resp.json().get("permitted", False)
    except Exception:
        logger.exception("Error checking WhatsApp call permission")
        return False


@auth_router.post("/send-call")
async def send_call(request: Request):
    """Place an outbound WhatsApp call or fall back to a call invite template.

    If the contact has granted call permission, places a direct outbound call
    via a Twilio Conference bridged to LiveKit.  Otherwise sends a VOICE_CALL
    template so the user can tap "Call now" to initiate an inbound call.
    """
    from datetime import datetime
    from common.livekit import ensure_phone_dispatch_rule, make_sip_uri

    data = await request.json()
    to = data["to"]
    assistant_id = data["assistant_id"]
    agent_name = data.get("agent_name", "")
    room_name = data["room_name"]

    route = await _resolve_route(assistant_id, to)
    pool_number = route["pool_number"]

    permitted = await _check_call_permission(pool_number, to)
    wa_client = get_twilio_wa_client()

    if permitted:
        sip_uri = make_sip_uri(pool_number)
        await ensure_phone_dispatch_rule(pool_number, room_name)

        date_time = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
        conference_name = f"Unity_WA_{pool_number[1:]}_{date_time}"

        from twilio.twiml.voice_response import VoiceResponse

        def _conference_twiml(conf_name: str) -> str:
            resp = VoiceResponse()
            dial = resp.dial()
            dial.conference(
                conf_name,
                startConferenceOnEnter=True,
                endConferenceOnExit=True,
                muted=False,
                wait_url="https://auburn-eagle-6359.twil.io/assets/ring-tone-68676.mp3",
            )
            return str(resp)

        user_call = wa_client.calls.create(
            to=f"whatsapp:{to}",
            from_=f"whatsapp:{pool_number}",
            twiml=_conference_twiml(conference_name),
            status_callback=SETTINGS.adapters_url + "/twilio/whatsapp-call-status",
            status_callback_event="initiated ringing answered completed",
        )
        wa_client.calls.create(
            to=sip_uri,
            from_=pool_number,
            twiml=_conference_twiml(conference_name),
        )
        logger.info(
            f"Outbound WhatsApp call placed to {to}. "
            f"Call SID: {user_call.sid}, Conference: {conference_name}",
        )
        return {
            "success": True,
            "method": "direct",
            "conference_name": conference_name,
        }

    wa_client.messages.create(
        content_sid=VOICE_CALL_REQUEST_TEMPLATE_SID,
        to=f"whatsapp:{to}",
        from_=f"whatsapp:{pool_number}",
        status_callback=f"{SETTINGS.comms_url}/whatsapp/status",
    )
    logger.info(f"WhatsApp call permission request sent to {to}")
    return {"success": True, "method": "invite"}


WHATSAPP_VOICE_APP_SID = (
    "APbf0903608f1a02e93bebcc90e2ea17db"
    if os.getenv("DEPLOY_ENV") == "staging"
    else "AP5e48f55135a987a482661a37db8ac68f"
)
WHATSAPP_GB_BUNDLE_SID = "BUd85f47e01a9d85003c364f400105a8da"

_SENDER_BASE = "https://messaging.twilio.com/v2/Channels/Senders"


async def _attach_voice_app(sender_sid: str, timeout: float = 60.0) -> bool:
    """Poll until the sender is ONLINE, then attach the TwiML Voice App.

    Returns True if the voice app was successfully attached, False on timeout
    or error.  Failures are non-fatal — the sender is still usable for
    messaging, just without WhatsApp Business Calling.
    """
    if not WHATSAPP_VOICE_APP_SID:
        return False

    headers = _twilio_whatsapp_auth_headers()
    sender_url = f"{_SENDER_BASE}/{sender_sid}"
    deadline = time.monotonic() + timeout

    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(sender_url, headers=headers, timeout=10.0)
                if resp.status_code < 400:
                    status = resp.json().get("status", "")
                    if status == "ONLINE":
                        break
                    logger.info(
                        f"Sender {sender_sid} status: {status}, waiting for ONLINE",
                    )
            except Exception:
                logger.exception(f"Error polling sender {sender_sid} status")
            await asyncio.sleep(5)
        else:
            logger.warning(
                f"Sender {sender_sid} did not reach ONLINE within {timeout}s",
            )
            return False

        try:
            update_resp = await client.post(
                sender_url,
                json={
                    "configuration": {
                        "voice_application_sid": WHATSAPP_VOICE_APP_SID,
                    }
                },
                headers=headers,
                timeout=10.0,
            )
            if update_resp.status_code >= 400:
                logger.error(
                    f"Failed to attach voice app to {sender_sid}: "
                    f"{update_resp.status_code} {update_resp.text}",
                )
                return False
            logger.info(
                f"Attached voice app {WHATSAPP_VOICE_APP_SID} to sender {sender_sid}",
            )
            return True
        except Exception:
            logger.exception(f"Error attaching voice app to sender {sender_sid}")
            return False


async def _provision_gb_phone_number() -> str:
    """Buy a GB mobile number with voice + SMS, configure webhooks,
    add to the Unity messaging service, and create a LiveKit SIP trunk.

    Returns the purchased E.164 number.
    """
    twilio_client = get_twilio_wa_client()

    numbers: list = []
    try:
        numbers += twilio_client.available_phone_numbers("GB").mobile.list(
            limit=1,
            sms_enabled=True,
            voice_enabled=True,
            beta=False,
        )
    except Exception:
        pass
    try:
        numbers += twilio_client.available_phone_numbers("GB").local.list(
            limit=1,
            sms_enabled=True,
            voice_enabled=True,
            beta=False,
        )
    except Exception:
        pass
    if not numbers:
        raise HTTPException(
            status_code=404,
            detail="No suitable GB phone numbers available",
        )

    record = numbers[0]
    incoming = twilio_client.incoming_phone_numbers.create(
        phone_number=record.phone_number,
        voice_url=SETTINGS.adapters_url + "/twilio/call",
        voice_method="POST",
        sms_url=SETTINGS.adapters_url + "/twilio/sms",
        sms_method="POST",
        status_callback=SETTINGS.adapters_url + "/twilio/call-status",
        status_callback_method="POST",
        bundle_sid=WHATSAPP_GB_BUNDLE_SID,
    )

    for service in twilio_client.messaging.v1.services.list():
        if service.friendly_name == "Unity":
            service.phone_numbers.create(phone_number_sid=incoming.sid)
            break

    lkapi = LiveKitAPI(
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    trunk = SIPInboundTrunkInfo(
        name=f"Unity_WA_{record.phone_number[1:]}",
        numbers=[record.phone_number],
        krisp_enabled=True,
    )
    await lkapi.sip.create_sip_inbound_trunk(
        CreateSIPInboundTrunkRequest(trunk=trunk),
    )
    await lkapi.aclose()

    logger.info(f"Provisioned GB number {record.phone_number} for WhatsApp sender")
    return record.phone_number


@auth_router.post("/create")
async def create_whatsapp_sender(request: Request):
    data = await request.json()
    phone_number = data.get("phone_number")

    if not phone_number:
        phone_number = await _provision_gb_phone_number()

    payload: dict = {
        "sender_id": f"whatsapp:{phone_number}",
        "profile": {
            "name": "Unify Assistant",
            "logo_url": "https://console.unify.ai/ivy_logo_only.png",
        },
        "webhook": {
            "callback_method": "POST",
            "callback_url": data.get(
                "callback_url",
                SETTINGS.adapters_url + "/twilio/whatsapp",
            ),
            "status_callback_url": SETTINGS.comms_url + "/whatsapp/status",
            "status_callback_method": "POST",
        },
    }

    headers = _twilio_whatsapp_auth_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://messaging.twilio.com/v2/Channels/Senders",
            json=payload,
            headers=headers,
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Failed to create WhatsApp sender: {resp.text}",
        )

    sid = resp.json().get("sid")
    calling_enabled = await _attach_voice_app(sid)

    return {
        "sid": sid,
        "phone_number": phone_number,
        "calling_enabled": calling_enabled,
    }


@auth_router.delete("/delete")
async def delete_whatsapp_sender(request: Request):
    data = await request.json()
    sid = data["sid"]
    url = f"https://messaging.twilio.com/v2/Channels/Senders/{sid}"
    headers = _twilio_whatsapp_auth_headers()
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Failed to delete WhatsApp sender: {resp.text}",
        )
    return {"success": True}


@auth_router.post("/assign")
async def assign_whatsapp_sender(request: Request):
    """Assign a pool number to an assistant via Orchestra."""
    data = await request.json()
    assistant_id = data["assistant_id"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SETTINGS.orchestra_url}/admin/whatsapp/assign",
            json={"assistant_id": assistant_id},
            headers=_admin_headers(),
            timeout=15.0,
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()
