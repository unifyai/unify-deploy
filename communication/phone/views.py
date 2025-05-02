import os
import unify
from fastapi import APIRouter, Form, Response, Request, HTTPException
from twilio.twiml.voice_response import VoiceResponse
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from livekit.api import LiveKitAPI
from livekit.api.sip_service import CreateSIPInboundTrunkRequest
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()
client = unify.Unify(traced=True)
client.set_endpoint("o4-mini@openai")
client.set_system_message("You are a helpful assistant.")

@router.post("/call")
async def call(To: str = Form(...)):
    phone_number = To or ""
    resp = VoiceResponse()
    dial = resp.dial()
    dial.sip(
        f"sip:+{phone_number[1:]}@{os.getenv('LIVEKIT_SIP_URI')}",
        username=os.getenv('TWIML_SIP_USERNAME'),
        password=os.getenv('TWIML_SIP_PASSWORD')
    )
    return Response(content=str(resp), media_type="text/xml")

@router.post("/text")
async def text_message(Body: str = Form(...)):
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

@router.post("/create")
async def create_phone_number():
    # Initialize Twilio client
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    client = TwilioClient(account_sid, auth_token)
    # Search for available US mobile number
    numbers = client.available_phone_numbers("US").local.list(
        limit=1, sms_enabled=True, voice_enabled=True
    )
    if not numbers:
        raise HTTPException(status_code=404, detail="No suitable phone numbers found.")
    record = numbers[0]
    # Purchase the number and configure webhooks
    incoming = client.incoming_phone_numbers.create(
        phone_number=record.phone_number,
        voice_url=f"{os.getenv('UNIFY_COMMS_URL')}/phone/call",
        voice_method="POST",
        sms_url=f"{os.getenv('UNIFY_COMMS_URL')}/phone/text",
        sms_method="POST",
    )
    # Set up LiveKit inbound SIP trunk
    lkapi = LiveKitAPI(
        os.getenv("LIVEKIT_URL"),
        os.getenv("LIVEKIT_API_KEY"),
        os.getenv("LIVEKIT_API_SECRET"),
    )
    provider_numbers = [record.phone_number]
    trunk_name = f"Unity_{record.phone_number[1:]}"
    sip_req = CreateSIPInboundTrunkRequest(
        name=trunk_name,
        numbers=provider_numbers,
        krisp_enabled=True,
        auth_username=os.getenv("TWIML_SIP_USERNAME"),
        auth_password=os.getenv("TWIML_SIP_PASSWORD"),
    )
    lkapi.sip.create_sip_inbound_trunk(sip_req)
    return {"success": True, "phoneNumber": incoming.phone_number}

@router.delete("/delete")
async def delete_phone_number(request: Request):
    # Expect JSON body: { "phoneNumber": "+1234567890" }
    data = await request.json()
    phone_number = data.get("phoneNumber")
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    client = TwilioClient(account_sid, auth_token)
    # Find the purchased number by E.164
    incoming_list = client.incoming_phone_numbers.list(
        phone_number=phone_number,
        limit=1
    )
    if not incoming_list:
        raise HTTPException(status_code=404, detail="Phone number not found")
    phone_sid = incoming_list[0].sid
    # Delete the number
    client.incoming_phone_numbers(phone_sid).delete()
    return {"success": True, "sid": phone_sid}