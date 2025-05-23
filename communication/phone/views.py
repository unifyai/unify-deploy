import os
import unify
import httpx
import base64
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

def create_conference_response(conference_name, with_status=False):
    resp_user = VoiceResponse()
    dial_user = resp_user.dial()
    if with_status:
        dial_user.conference(
            conference_name,
            startConferenceOnEnter=True,
            endConferenceOnExit=True,
            muted=False,
            record="record-from-start",
            recording_status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/recording",
            recording_status_callback_event='completed',
            status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/call-status",
            status_callback_event=["completed"]
        )
        return resp_user
    
    dial_user.conference(
        conference_name,
        startConferenceOnEnter=True,
        endConferenceOnExit=True,
        muted=False,
        record="record-from-start",
        recording_status_callback=f"{os.getenv('UNIFY_COMMS_URL')}/phone/recording",
        recording_status_callback_event='completed'
    )
    return resp_user

def add_user_to_conference(conference_name, from_number, to_number_uri, connect_third_party=False):
    twilio_client = get_twilio_client()

    if connect_third_party:
        conferences = twilio_client.conferences.list(friendly_name=conference_name, status="in-progress")
        participants = twilio_client.conferences(conferences[0].sid).participants.list()
        for participant in participants:
            call = twilio_client.calls(participant.call_sid).fetch()
            # Identify Livekit Agent and mute
            if "livekit.cloud" in call.to:
                twilio_client.conferences(conferences[0].sid).participants(participant.sid).update(muted=True)
                break 
        response = create_conference_response(conference_name, with_status=True)
    else:
        response = create_conference_response(conference_name)

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
    resp_user = create_conference_response(conference_name)
    # Put user into conference
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
async def check_recording_status(
    RecordingUrl: str = Form(...), 
    ConferenceSid: str = Form(...), 
    ParticipantSid: str = Form(...),
):
    recording_url = RecordingUrl or ""
    conference_sid = ConferenceSid or ""
    participant_sid = ParticipantSid or ""
    if not recording_url:
        return {"success": False, "error": "RecordingUrl is required"}
    
    # Get recording from Twilio
    recording_url = recording_url + ".mp3"
    async with httpx.AsyncClient(
        auth=(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    ) as httpx_client:
        resp = httpx_client.get(recording_url)
    if resp.status_code >= 400:
        raise HTTPException(resp.status.code)
    
    # Extract recording bytes
    resp_bytes = resp.content
    resp_bytes = base64.b64encode(resp_bytes).decode('utf-8')
    headers = {
        "Authorization": f"Bearer {os.environ["UNIFY_KEY"]}",
        "Content-Type": "application/json"
    }

    # Get number through Twilio ConferenceSid and ParticipantSid
    twilio_client = get_twilio_client()
    participant = twilio_client.conferences(conference_sid).participants(participant_sid).fetch()
    call_sid = participant.call_sid
    call = twilio_client.calls(call_sid).fetch()

    # assistant_id (get from unify api thorugh phone number search)
    async with httpx.AsyncClient() as httpx_client:
        resp = httpx_client.post(
            f"https://api.unify.ai/v0/assistant",
            headers=headers,
        )
    if resp.status_code >= 400:
        raise HTTPException(resp.status.code)
    assistants = resp.json()["info"]
    for assistant in assistants:
        if assistant["phone"] in (call.from_, call.to):
            assistant_id = assistant["agent_id"]
            break
    
    payload = {
        "recording_raw": resp_bytes,
        "content_type": "audio/mp3",
    }
    async with httpx.AsyncClient() as httpx_client:
        resp = httpx_client.post(
            f"https://api.unify.ai/v0/assistant/{assistant_id}/recordings",
            headers=headers,
            json=payload,
        )
    if resp.status_code >= 400:
        raise HTTPException(resp.status.code)
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
    
    call_sid = add_user_to_conference(conference_name, twilio_number, phone_number, connect_third_party=(not new_call))
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
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
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
    await lkapi.aclose()
    return {"success": True, "phoneNumber": incoming.phone_number}

@router.delete("/delete")
async def delete_phone_number(request: Request):
    # Expect JSON body: { "PhoneNumber": "+1234567890" }
    data = await request.json()
    phone_number = data.get("PhoneNumber")
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
        url=os.getenv("LIVEKIT_URL"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )
    sip_its = await lkapi.sip.list_sip_inbound_trunk(ListSIPInboundTrunkRequest())
    for item in sip_its.items:
        if phone_number[1:] in item.name:
            await lkapi.sip.delete_sip_trunk(DeleteSIPTrunkRequest(sip_trunk_id=item.sip_trunk_id))
            break
    
    await lkapi.aclose()
    return {"success": True, "sid": phone_sid}

# @router.post("/press")
# async def press_key(request: Request):
#     data = await request.json()
#     to = data.get("To")
#     frm = data.get("From")
#     digits = data.get("Digits")
#     call_sid = data.get("CallSid") 

#     twilio_client = get_twilio_client()
#     twilio_client.calls(call_sid).update(send_digits=digits)
#     return {"success": True}

@router.post("/hang-up")
async def call_status(request: Request):
    data = await request.json()
    call_sid = data.get("CallSid")

    twilio_client = get_twilio_client()
    conference = twilio_client.conferences(call_sid).update(status="completed")
    return {"success": True, "status": conference.status}

@router.post("/call-status")
async def call_status(request: Request):
    data = await request.json()
    call_status = data.get("CallStatus")
    conference_sid = data.get("ConferenceSid")

    twilio_client = get_twilio_client()
    if call_status == "completed":
        # Unmute LiveKit agent (Agent A) after User B hangs up
        participants = twilio_client.conferences(conference_sid).participants.list()
        for participant in participants:
                twilio_client.conferences(conference_sid).participants(participant.sid).update(muted=False)
    return Response(status=200)