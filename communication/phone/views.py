import os
import httpx
import base64
import json
import time
from fastapi import APIRouter, Response, Request, HTTPException
from twilio.twiml.voice_response import VoiceResponse
from livekit.api import (
    LiveKitAPI,
    SIPInboundTrunkInfo,
    CreateSIPInboundTrunkRequest,
    CreateAgentDispatchRequest,
)
from livekit.protocol.sip import (
    ListSIPInboundTrunkRequest,
    DeleteSIPTrunkRequest,
    CreateSIPParticipantRequest,
)
from communication.helpers import get_twilio_client, ORCHESTRA_URL
from dotenv import load_dotenv

load_dotenv()

auth_router = APIRouter()
unauth_router = APIRouter()


# Helpers
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
            recording_status_callback=f"{os.getenv('UNITY_COMMS_URL')}/phone/recording",
            recording_status_callback_event="completed",
            status_callback=f"{os.getenv('UNITY_COMMS_URL')}/phone/call-status",
            status_callback_event=["completed"],
        )
        return resp_user

    dial_user.conference(
        conference_name,
        startConferenceOnEnter=True,
        endConferenceOnExit=True,
        muted=False,
        record="record-from-start",
        recording_status_callback=f"{os.getenv('UNITY_COMMS_URL')}/phone/recording",
        recording_status_callback_event="completed",
    )
    return resp_user


def add_user_to_conference(
    conference_name, from_number, to_number_uri, connect_third_party=False
):
    twilio_client = get_twilio_client()

    if connect_third_party:
        conferences = twilio_client.conferences.list(
            friendly_name=conference_name, status="in-progress"
        )
        participants = twilio_client.conferences(conferences[0].sid).participants.list()
        for participant in participants:
            call = twilio_client.calls(participant.call_sid).fetch()
            # Identify Livekit Agent and mute
            if "livekit.cloud" in call.to:
                twilio_client.conferences(conferences[0].sid).participants(
                    participant.sid
                ).update(muted=True)
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
@unauth_router.post("/recording")
async def check_recording_status(request: Request):
    data = await request.form()

    print("Recorded data")
    for key, value in data.items():
        print(key, value)
    recording_url = data.get("RecordingUrl")

    if not recording_url:
        return {"success": False, "error": "RecordingUrl is required"}

    # Get recording from Twilio
    recording_url = recording_url + ".mp3"
    async with httpx.AsyncClient(
        auth=(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    ) as httpx_client:
        resp = await httpx_client.get(recording_url)
    if resp.status_code >= 400:
        print("Failed to get recording from Twilio")
        raise HTTPException(
            status_code=resp.status_code, detail="Failed to get recording from Twilio"
        )

    # Extract recording bytes
    resp_bytes = resp.content
    resp_bytes = base64.b64encode(resp_bytes).decode("utf-8")
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}

    # Get number through Twilio RecordingSid or Conference participants
    twilio_client = get_twilio_client()
    recording_sid = data.get("RecordingSid")
    call = None

    # Get call info from recording
    recording = twilio_client.recordings(recording_sid).fetch()
    call_sid = recording.call_sid
    call = twilio_client.calls(call_sid).fetch()

    print("Call: ", call)
    print("Call from: ", call._from)
    print("Call to: ", call.to)
    print("Call sid: ", call.sid)
    print("Call status: ", call.status)
    print("Call duration: ", call.duration)
    print("Call start time: ", call.start_time)

    # assistant_id (get from unify api thorugh phone number search)
    async with httpx.AsyncClient() as httpx_client:
        resp = await httpx_client.get(
            f"{ORCHESTRA_URL}/admin/assistant",
            params={"phone": call._from},
            headers=headers,
        )
        assistants = resp.json()["info"]
        if len(assistants) == 0:
            resp = await httpx_client.get(
                f"{ORCHESTRA_URL}/admin/assistant",
                params={"phone": call.to},
                headers=headers,
            )
            assistants = resp.json()["info"]
    if resp.status_code >= 400:
        print("Failed to get assistants from Unify")
        raise HTTPException(
            status_code=resp.status_code, detail="Failed to get assistants from Unify"
        )
    for assistant in assistants:
        if assistant["phone"] in [call._from, call.to]:
            assistant_id = assistant["agent_id"]
            user_id = assistant["user_id"]
            break

    payload = {
        "recording_raw": resp_bytes,
        "content_type": "audio/mp3",
        "assistant_id": assistant_id,
        "user_id": user_id,
    }
    async with httpx.AsyncClient() as httpx_client:
        resp = await httpx_client.post(
            f"{ORCHESTRA_URL}/admin/assistant/recordings",
            headers=headers,
            json=payload,
        )
    if resp.status_code >= 400:
        print("Failed to upload recording to Unify")
        print(resp.text)
        raise HTTPException(
            status_code=resp.status_code, detail="Failed to upload recording to Unify"
        )
    return {"success": True, "recording_url": recording_url}


def get_livekit_api():
    """Get LiveKit API client"""
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not url or not api_key or not api_secret:
        raise RuntimeError(
            "LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET must be set"
        )

    return LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)


async def create_room_and_dispatch_agent(
    room_name: str, agent_name: str, metadata: dict = None
):
    """Create a LiveKit room and dispatch an agent to it"""
    livekit_api = get_livekit_api()

    try:
        # Create dispatch request - this will create the room if it doesn't exist
        dispatch_request = CreateAgentDispatchRequest(
            agent_name=agent_name,
            room=room_name,
            metadata=json.dumps(metadata) if metadata else None,
        )

        # Dispatch agent to room (creates room automatically if needed)
        dispatch = await livekit_api.agent_dispatch.create_dispatch(dispatch_request)
        print(
            f"Successfully created room '{room_name}' and dispatched agent '{agent_name}'"
        )
        print(f"Dispatch ID: {dispatch.id}")

        return dispatch
    except Exception as e:
        print(f"Error creating room and dispatching agent: {str(e)}")
        raise
    finally:
        await livekit_api.aclose()


# Endpoints - JSON format
@auth_router.post("/dispatch-agent")
async def dispatch_agent(request: Request):
    data = await request.json()
    agent_name = data.get("agent_name")
    await create_room_and_dispatch_agent(agent_name, agent_name)
    return {"success": True}


@auth_router.post("/send-call")
async def send_call(request: Request):
    data = await request.json()
    phone_number = data.get("To")
    twilio_number = data.get("From")
    new_call = data.get("NewCall")

    new_call = new_call.lower() == "true"
    room_name = f"unity_{twilio_number}"

    # create livekit agent participant
    lkapi = LiveKitAPI()
    print("creating call")
    trunk = CreateSIPParticipantRequest(
        sip_trunk_id="ST_knkas2oxiawB",
        sip_number=twilio_number,
        sip_call_to=phone_number,
        room_name=room_name,
        participant_identity=f"user_{phone_number}",
        participant_name="User",
        wait_until_answered=True,
    )
    print("answered")
    call = await lkapi.sip.create_sip_participant(trunk)

    # add user to twilio conference
    # sip_uri = f"sip:+{twilio_number[1:]}@{os.getenv('LIVEKIT_SIP_URI')}"
    # # call_sid = add_user_to_conference(conference_name, phone_number, sip_uri)
    # call_sid = add_user_to_conference(conference_name, twilio_number, phone_number)
    return {"success": True}  # , "call_sid": call_sid}


@auth_router.post("/send-text")
async def send_text(request: Request):
    data = await request.json()
    To = data.get("To")
    From = data.get("From")
    Body = data.get("Body")

    twilio_client = get_twilio_client()
    twilio_client.messages.create(to=To, from_=From, body=Body)
    return {"success": True}


@auth_router.post("/meet-call")
async def send_meet_call(request: Request):
    data = await request.json()
    meet_id = data.get("meet_id")
    phone_number = data.get("to")
    twilio_number = data.get("from")

    conference_name = f"Unity_{twilio_number[1:]}"
    room_name = meet_id

    # dispatch agent
    await create_room_and_dispatch_agent(
        room_name=room_name,
        agent_name=room_name,
        metadata={
            "caller_number": phone_number,
            "twilio_number": twilio_number,
            "conference_name": conference_name,
            "call_type": "inbound",
            "call_sid": None,  # Will be updated after conference setup
            "timestamp": int(time.time() * 1000),
        },
    )

    return {"success": True}


@auth_router.get("/available-countries")
async def available_countries():
    return {"success": True, "countries": "US,GB,AU,CA,FI,NL,PR,TH,PL"}


@auth_router.post("/create")
async def create_phone_number(request: Request):
    data = await request.json()

    # Extract customizable parameters from request
    voice_url = data.get("voice_url", "https://us-central1-gcp-project-runtime.cloudfunctions.net/twilio-call-webhook")
    sms_url = data.get("sms_url", "https://us-central1-gcp-project-runtime.cloudfunctions.net/twilio-msg-webhook")
    country = data.get("country", "US")

    # Additional args for country
    additional_args = {}
    if country == "GB":
        additional_args["bundle_sid"] = "BU92b4971def01df8ce390153e23645323"
    elif country in ["NL", "FI"]:
        additional_args["address_sid"] = "AD742b83eb0aab7a249e7a3f2f5fb615c0"
    elif country == "AU":
        additional_args["bundle_sid"] = "BUd8f2d4e2fe905d85653f738d7323c88b"
        additional_args["address_sid"] = "AD828c09f385dea4f977464da90006bfd7"
    elif country == "TH":
        additional_args["bundle_sid"] = "BUadbcfca4db22f76c6840ced254c10a11"
        additional_args["address_sid"] = "ADdf839edff37d001d2634edc9b0c4a304"
    elif country == "PL":
        additional_args["bundle_sid"] = "BU0864466d980ebd9df91768d9123110b2"
        additional_args["address_sid"] = "ADdf839edff37d001d2634edc9b0c4a304"

    # Initialize Twilio client
    twilio_client = get_twilio_client()

    # Search for available mobile number
    numbers = []
    try:
        numbers += twilio_client.available_phone_numbers(country).local.list(
            limit=1, sms_enabled=True, voice_enabled=True, beta=False
        )
    except Exception as e:
        pass

    try:
        numbers += twilio_client.available_phone_numbers(country).mobile.list(
            limit=1, sms_enabled=True, voice_enabled=True, beta=False
        )
    except Exception as e:
        pass

    if not numbers:
        raise HTTPException(status_code=404, detail="No suitable phone numbers found.")
    record = numbers[0]

    # Purchase the number and configure webhooks
    incoming = twilio_client.incoming_phone_numbers.create(
        phone_number=record.phone_number,
        voice_url=voice_url,
        voice_method="POST",
        sms_url=sms_url,
        sms_method="POST",
        **additional_args,
    )

    # Set up the messaging service
    services = twilio_client.messaging.v1.services.list()
    for service in services:
        if service.friendly_name == "Unity":
            service.phone_numbers.create(phone_number_sid=incoming.sid)
            break

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


@auth_router.delete("/delete")
async def delete_phone_number(request: Request):
    # Expect JSON body: { "PhoneNumber": "+1234567890" }
    data = await request.json()
    phone_number = data.get("PhoneNumber")
    twilio_client = get_twilio_client()

    # Find the purchased number by E.164
    incoming_list = twilio_client.incoming_phone_numbers.list(
        phone_number=phone_number, limit=1
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
            await lkapi.sip.delete_sip_trunk(
                DeleteSIPTrunkRequest(sip_trunk_id=item.sip_trunk_id)
            )
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


@auth_router.post("/hang-up")
async def hang_up(request: Request):
    data = await request.json()
    call_sid = data.get("CallSid")
    conference_name = data.get("ConferenceName")

    twilio_client = get_twilio_client()
    conferences = twilio_client.conferences.list(
        friendly_name=conference_name, status="in-progress"
    )
    conference = (
        twilio_client.conferences(conferences[0].sid).participants(call_sid).delete()
    )
    return Response(status=200)


@auth_router.post("/end-conference")
async def end_conference(request: Request):
    data = await request.json()
    conference_name = data.get("ConferenceName")

    twilio_client = get_twilio_client()
    conferences = twilio_client.conferences.list(
        friendly_name=conference_name, status="in-progress"
    )
    conference = twilio_client.conferences(conferences[0].sid).update(
        status="completed"
    )
    return {"success": True, "status": conference.status}


@unauth_router.post("/call-status")
async def call_status(request: Request):
    data = await request.json()
    call_status = data.get("CallStatus")
    conference_sid = data.get("ConferenceSid")

    twilio_client = get_twilio_client()
    if call_status == "completed":
        # Unmute LiveKit agent (Agent A) after User B hangs up
        participants = twilio_client.conferences(conference_sid).participants.list()
        for participant in participants:
            twilio_client.conferences(conference_sid).participants(
                participant.sid
            ).update(muted=False)
    return Response(status=200)
