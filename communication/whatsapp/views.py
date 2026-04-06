import base64
import json
import logging
import os

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request

from communication.helpers import get_twilio_wa_client
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
                    "message": body,
                },
            ),
            status_callback=f"{SETTINGS.comms_url}/whatsapp/status",
        )
        method = "template"

    return {"success": True, "method": method}


@auth_router.post("/create")
async def create_whatsapp_sender(request: Request):
    data = await request.json()
    url = "https://messaging.twilio.com/v2/Channels/Senders"
    payload = {
        "sender_id": f"whatsapp:{data['phone_number']}",
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
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Failed to create WhatsApp sender: {resp.text}",
        )
    return {"sid": resp.json().get("sid")}


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
