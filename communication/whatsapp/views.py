import os
import base64
import httpx
from fastapi import APIRouter, Request, HTTPException
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

@router.post("/whatsapp/senders")
async def create_whatsapp_sender(request: Request):
    data = await request.json()
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    url = "https://messaging.twilio.com/v2/Channels/Senders"
    payload = {
        "sender_id": f"whatsapp:{data.get('phone_number')}",
        "profile": {"name": f"{data.get('first_name')} {data.get('surname')}"},
        "webhook": {
            "callback_method": "POST",
            "callback_url": f"{os.getenv('UNIFY_COMMS_URL')}/phone/text"
        }
    }
    auth_str = f"{account_sid}:{auth_token}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {
        "Authorization": f"Bearer {b64_auth}",
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        text = await resp.text()
        raise HTTPException(status_code=resp.status_code, detail=f"Failed to create WhatsApp sender: {text}")
    resp_data = resp.json()
    return {"sid": resp_data.get("sid")}

@router.delete("/whatsapp/senders/{sid}")
async def delete_whatsapp_sender(sid: str):
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    url = f"https://messaging.twilio.com/v2/Channels/Senders/{sid}"
    auth_str = f"{account_sid}:{auth_token}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {
        "Authorization": f"Bearer {b64_auth}"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.delete(url, headers=headers)
    if resp.status_code >= 400:
        text = await resp.text()
        raise HTTPException(status_code=resp.status_code, detail=f"Failed to delete WhatsApp sender: {text}")
    return {"success": True}