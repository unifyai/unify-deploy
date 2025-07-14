import os
import json
import unify
import base64
import httpx
from fastapi import APIRouter, Form, Response, Request, HTTPException
from dotenv import load_dotenv
from twilio.rest import Client as TwilioClient

load_dotenv()

router = APIRouter()
client = unify.Unify(api_key=os.getenv("UNIFY_KEY"))
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
async def receive_text(
    To: str = Form(...),
    From: str = Form(...),
    Body: str = Form(...),
):
    # Extract message body
    twilio_number = To or ""
    sender_number = From or ""
    body = Body or ""

    # Prepare chat messages
    messages = [
        {"role": "user", "content": body},
    ]
    # Call Unify ChatCompletion
    response = client.generate(
        messages=messages,
    )

    # Send message with Twilio client to ensure status monitoring
    twilio_client = get_twilio_client()
    twilio_client.messages.create(
        to=sender_number,
        from_=twilio_number,
        body=response,
        status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/whatsapp/status",
    )

    return Response(status=200)

@router.post("/status")
async def check_whatsapp_status(
    MessageStatus: str = Form(...), 
    To: str = Form(...), 
    From: str = Form(...),
):
    to = To or ""
    frm = From or ""
    msg_status = MessageStatus or ""
    return {
        "status": True, 
        "message_status": msg_status, 
        "to_number": to, 
        "from_number": frm,
    }

# Endpoints - JSON format
@router.post("/send-text")
async def send_text(request: Request):
    data = await request.json()
    receiver_number = data.get("to")
    twilio_number = data.get("from")
    body = data.get("body")

    twilio_client = get_twilio_client()
    twilio_client.messages.create(
        to=f"whatsapp:{receiver_number}",
        from_=f"whatsapp:{twilio_number}",
        body=body,
        status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/whatsapp/status",
    )
    return {"success": True}

@router.post("/send-greeting")
async def send_greeting(request: Request):
    data = await request.json()
    receiver_number = data.get("to")
    twilio_number = data.get("from")
    user_name = data.get("user_name")
    agent_name = data.get("agent_name")

    twilio_client = get_twilio_client()
    twilio_client.messages.create(
        content_sid="HXe1624dc6761e564cf974d214278d0a6f",
        to=f"whatsapp:{receiver_number}",
        from_=f"whatsapp:{twilio_number}",
        content_variables=json.dumps({
            "user_name": user_name,
            "agent_name": agent_name
        }),
        status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/whatsapp/status",
    )
    return {"success": True}

@router.post("/create")
async def create_whatsapp_sender(request: Request):
    data = await request.json()
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    url = "https://messaging.twilio.com/v2/Channels/Senders"
    payload = {
        "sender_id": f"whatsapp:{data.get('phone_number')}",
        "profile": {
            "name": "Unify Assistant",
            "logo_url": "https://console.unify.ai/ivy_logo_only.png",
        },
        "webhook": {
            "callback_method": "POST",
            "callback_url": data.get(
                "callback_url",
                f"{os.getenv('UNITY_COMMS_URL')}/whatsapp/text"
            )
        }
    }
    auth_str = f"{account_sid}:{auth_token}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/json"
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"Failed to create WhatsApp sender: {resp.text}")
    resp_data = resp.json()
    return {"sid": resp_data.get("sid")}

@router.delete("/delete")
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

@router.post("/assign")
async def assign_whatsapp_sender(request: Request):
    data = await request.json()
    user_whatsapp_number = data.get("user_whatsapp_number")
    conflict_whatsapp_number = data.get("conflict_whatsapp_number", None)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.unify.ai/v0/admin/assistant?user_whatsapp_number={user_whatsapp_number}",
            headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"Failed to fetch assistants: {resp.text}")
    resp_data = resp.json()
    assistants_whatsapp_numbers = [assistant["whatsapp_number"] for assistant in resp_data["info"]]
    if conflict_whatsapp_number:
        assistants_whatsapp_numbers += [conflict_whatsapp_number]
    
    # no twilio api for listing whatsapp numbers, manual for now
    all_whatsapp_numbers = ["+15550100001", "+15550100002"]
    available_whatsapp_number = None
    for number in all_whatsapp_numbers:
        if number not in assistants_whatsapp_numbers:
            print(f"Whatsapp number {number} is not assigned to any assistant")
            available_whatsapp_number = number
            break

    if not available_whatsapp_number:
        raise HTTPException(status_code=400, detail="No available WhatsApp number found")

    return {"whatsapp_number": available_whatsapp_number}

@router.get("/conflict")
async def get_conflict_whatsapp_number(request: Request):
    data = await request.json()
    user_id = data.get("user_id")
    assistant_whatsapp_number = data.get("assistant_whatsapp_number")
    target_whatsapp_number = data.get("target_whatsapp_number")

    # search if target has an assistant with the same whatsapp number
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.unify.ai/v0/admin/assistant?user_whatsapp_number={target_whatsapp_number}&assistant_whatsapp_number={assistant_whatsapp_number}",
            headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"Failed to fetch assistants: {resp.text}")
    resp_data = resp.json()
    found_assistants = resp_data.get("info", [])
    if found_assistants:
        return {"conflict": "both"}
    
    # search if target is in any other user's contact list
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.unify.ai/v0/admin/contacts?whatsapp_number={target_whatsapp_number}",
            headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"Failed to fetch assistants: {resp.text}")
    found_contacts = resp.json()
    if found_contacts:
        found_target_user_ids = set([contact["user_id"] for contact in found_contacts])
        for uid in found_target_user_ids:
            if uid == user_id:
                continue
            # check if user has an assistant
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://api.unify.ai/v0/admin/assistant/user/{uid}&assistant_whatsapp_number={assistant_whatsapp_number}",
                    headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
                )
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=f"Failed to fetch assistants: {resp.text}")
            resp_data = resp.json()
            if resp_data.get("info", []):
                return {"conflict": "single"}
    
    # no conflict found
    return {"conflict": "none"}