import os
import unify
from fastapi import APIRouter, Form, Response, Request, HTTPException
from twilio.twiml.voice_response import VoiceResponse
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from livekit.api import LiveKitAPI, SIPInboundTrunkInfo, CreateSIPInboundTrunkRequest
from livekit.protocol.sip import ListSIPInboundTrunkRequest, DeleteSIPTrunkRequest
from dotenv import load_dotenv

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

def add_user_to_conference(conference_name, from_number, to_number_uri):
    twilio_client = get_twilio_client()

    response = VoiceResponse()
    dial = response.dial()
    dial.conference(
        conference_name,
        startConferenceOnEnter=True,
        endConferenceOnExit=True,
        muted=False,
        record="record-from-start",
        recording_status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/recording",
        recording_status_callback_event='completed',
    )
    response.append(dial)

    call = twilio_client.calls.create(
        to=to_number_uri,
        from_=from_number, 
        twiml=str(response),
    )
    return call.sid

# Endpoints - Form format
@router.post("/call")
async def receive_call(To: str = Form(...), From: str = Form(...)):
    twilio_number = To or ""
    caller_number = From or ""

    conference_name = f"Unity_{twilio_number[1:]}"
    sip_uri = f"sip:+{twilio_number[1:]}@{os.getenv('LIVEKIT_SIP_URI')}"

    # Put inbound caller into conference
    resp_user = VoiceResponse()
    dial_user = resp_user.dial()
    dial_user.conference(
        conference_name,
        startConferenceOnEnter=True,
        endConferenceOnExit=True,
        muted=False,
        record="record-from-start",
        recording_status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/recording",
        recording_status_callback_event='completed'
    )

    call_sid = add_user_to_conference(conference_name, caller_number, sip_uri)
    return Response(content=str(resp_user), media_type="text/xml")

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

@router.post("/recording")
async def check_recording_status(RecordingUrl: str = Form(...)):
    recording_url = RecordingUrl or ""
    if not recording_url:
        return {"success": False, "error": "RecordingUrl is required"}
    
    # todo: download link = recording_url, call db endpoint to store
    return {"success": True, "recording_url": recording_url}

# Endpoints - JSON format
@router.post("/send-call")
async def send_call(request: Request):
    data = await request.json()
    phone_number = data.get("To")
    twilio_number = data.get("From")
    new_call = data.get("NewCall")
    
    new_call = new_call.lower() == "true"
    conference_name = f"Unity_{twilio_number[1:]}"

    if new_call:
        sip_uri = f"sip:+{twilio_number[1:]}@{os.getenv('LIVEKIT_SIP_URI')}"
        call_sid = add_user_to_conference(conference_name, twilio_number, sip_uri)
    
    call_sid = add_user_to_conference(conference_name, twilio_number, phone_number)
    return {"success": True, "call_sid": call_sid}

@router.post("/send-text")
async def send_text(request: Request):
    data = await request.json()
    To = data.get("To")
    From = data.get("From")
    Body = data.get("Body")

    twilio_client = get_twilio_client()
    twilio_client.messages.create(
        to=To,
        from_=From,
        body=Body
    )
    return {"success": True}

@router.post("/create")
async def create_phone_number():
    # Initialize Twilio client
    twilio_client = get_twilio_client()
    # Search for available US mobile number
    numbers = twilio_client.available_phone_numbers("US").local.list(
        limit=1, sms_enabled=True, voice_enabled=True
    )
    if not numbers:
        raise HTTPException(status_code=404, detail="No suitable phone numbers found.")
    record = numbers[0]
    # Purchase the number and configure webhooks
    incoming = twilio_client.incoming_phone_numbers.create(
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
    sip_trunk = SIPInboundTrunkInfo(
        name=trunk_name,
        numbers=provider_numbers,
        krisp_enabled=True,
    )
    sip_req = CreateSIPInboundTrunkRequest(trunk=sip_trunk)
    await lkapi.sip.create_sip_inbound_trunk(sip_req)
    return {"success": True, "phoneNumber": incoming.phone_number}

@router.delete("/delete")
async def delete_phone_number(request: Request):
    # Expect JSON body: { "phoneNumber": "+1234567890" }
    data = await request.json()
    phone_number = data.get("phoneNumber")
    twilio_client = get_twilio_client()
    # Find the purchased number by E.164
    incoming_list = twilio_client.incoming_phone_numbers.list(
        phone_number=phone_number,
        limit=1
    )
    if not incoming_list:
        raise HTTPException(status_code=404, detail="Phone number not found")
    phone_sid = incoming_list[0].sid
    # Delete the number
    twilio_client.incoming_phone_numbers(phone_sid).delete()

    # Delete LiveKit SIP Trunk
    lkapi = LiveKitAPI(
        os.getenv("LIVEKIT_URL"),
        os.getenv("LIVEKIT_API_KEY"),
        os.getenv("LIVEKIT_API_SECRET"),
    )
    sip_its = await lkapi.sip.list_sip_inbound_trunk(ListSIPInboundTrunkRequest())
    for item in sip_its.items:
        if phone_number[1:] in item.name:
            await lkapi.sip.delete_sip_trunk(DeleteSIPTrunkRequest(sip_trunk_id=item.sip_trunk_id))
            break

    return {"success": True, "sid": phone_sid}

@router.post("/press")
async def press_key(request: Request):
    data = await request.json()
    To = data.get("To")
    From = data.get("From")
    Digits = data.get("Digits")
    call_sid = data.get("CallSid") # todo!

    twilio_client = get_twilio_client()
    twilio_client.calls(call_sid).update(send_digits=Digits)
    return {"success": True}
