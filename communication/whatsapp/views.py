import os
import unify
import base64
import httpx
from fastapi import APIRouter, Form, Response, Request, HTTPException
from dotenv import load_dotenv
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient

load_dotenv()

router = APIRouter()
client = unify.Unify(traced=True)
client.set_endpoint("o4-mini@openai")
client.set_system_message("You are a helpful assistant.")

# Helpers
def get_twilio_client():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)

# Endpoints - Form format
@router.post("/text")
async def receive_text(Body: str = Form(...)):
    # Extract message body
    body = Body or ""
    # Prepare chat messages
    messages = [
        {"role": "user", "content": body},
    ]
    # Call Unify ChatCompletion
    response = client.generate(
        messages=messages,
    )
    # Extract AI reply
    reply_text = response
    # Build TwiML messaging response
    twiml_resp = MessagingResponse()
    twiml_resp.message(reply_text)
    # Return XML
    return Response(content=str(twiml_resp), media_type="text/xml")

@router.post("/status")
async def check_whatsapp_status(
    MessageStatus: str = Form(...), 
    To: str = Form(...), 
    From: str = Form(...),
):
    MessageStatus = MessageStatus or ""
    return {"status": True, "message_status": MessageStatus}

# Endpoints - JSON format
@router.post("/send-text")
async def send_text(request: Request):
    data = await request.json()
    to = data.get("to")
    frm = data.get("from")
    body = data.get("body")

    twilio_client = get_twilio_client()
    twilio_client.messages.create(
        to=f"whatsapp:{to}",
        from_=f"whatsapp:{frm}",
        body=body,
        status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/whatsapp/status",
    )
    return {"success": True}

@router.post("/senders")
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

@router.delete("/senders")
async def delete_whatsapp_sender(request: Request):
    data = await request.json()
    sid = data.get("sid")
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
