import os
from fastapi import APIRouter, Response, Request, HTTPException
from twilio.twiml.voice_response import VoiceResponse
from livekit.api import (
    LiveKitAPI,
    SIPInboundTrunkInfo,
    CreateSIPInboundTrunkRequest,
)
from livekit.protocol.sip import (
    ListSIPInboundTrunkRequest,
    DeleteSIPTrunkRequest,
)
from common.livekit import (
    create_room_and_dispatch_agent,
    ensure_phone_dispatch_rule,
    make_sip_uri,
)
from communication.helpers import ADAPTERS_URL, get_twilio_client
from dotenv import load_dotenv

load_dotenv()

auth_router = APIRouter()
unauth_router = APIRouter()


# Helpers
def create_conference_response(conference_name, sip_uri, with_status=False):
    resp_user = VoiceResponse()
    dial_user = resp_user.dial()
    dial_user.sip(
        sip_uri,
        status_callback=f"{os.getenv('UNITY_COMMS_URL')}/phone/sip-status",
        status_callback_event="initiated ringing answered completed",
    )
    return resp_user


def add_user_to_conference(
    conference_name,
    from_number,
    to_number,
    sip_uri,
    connect_third_party=False,
):
    twilio_client = get_twilio_client()

    if connect_third_party:
        conferences = twilio_client.conferences.list(
            friendly_name=conference_name,
            status="in-progress",
        )
        participants = twilio_client.conferences(conferences[0].sid).participants.list()
        for participant in participants:
            call = twilio_client.calls(participant.call_sid).fetch()
            # Identify Livekit Agent and mute
            if "livekit.cloud" in call.to:
                twilio_client.conferences(conferences[0].sid).participants(
                    participant.sid,
                ).update(muted=True)
                break
        response = create_conference_response(
            conference_name,
            sip_uri,
            with_status=True,
        )
    else:
        response = create_conference_response(conference_name, sip_uri)

    print("TWIML RESPONSE:", str(response))
    call = twilio_client.calls.create(
        to=to_number,
        from_=from_number,
        twiml=str(response),
    )
    return call.sid


# Endpoints - JSON format
@auth_router.post("/dispatch-livekit-agent")
async def dispatch_livekit_agent(request: Request):
    data = await request.json()
    room_name = data.get("room_name") or data.get("livekit_agent_name", "")
    await create_room_and_dispatch_agent(
        room_name,
        room_name,
        record=data.get("record", False),
        assistant_id=data.get("assistant_id", ""),
        user_id=data.get("user_id", ""),
    )
    return {"success": True}


@auth_router.post("/send-call")
async def send_call(request: Request):
    data = await request.json()
    phone_number = data.get("To")
    twilio_number = data.get("From")
    room_name = data.get("room_name")
    sip_uri = make_sip_uri(twilio_number)
    await ensure_phone_dispatch_rule(twilio_number, room_name)
    twilio_client = get_twilio_client()
    call = twilio_client.calls.create(
        to=sip_uri,
        from_=twilio_number,
        url=f"{os.getenv('UNITY_COMMS_URL')}/phone/twiml?phone_number={phone_number}",
    )
    return {"success": True, "call_sid": call.sid}


@auth_router.post("/send-text")
async def send_text(request: Request):
    data = await request.json()
    To = data.get("To")
    From = data.get("From")
    Body = data.get("Body")

    twilio_client = get_twilio_client()
    twilio_client.messages.create(to=To, from_=From, body=Body)
    return {"success": True}


@auth_router.get("/available-countries")
async def available_countries():
    return {"success": True, "countries": "US,GB,AU,CA,FI,NL,PR,TH,PL"}


@auth_router.post("/create")
async def create_phone_number(request: Request):
    data = await request.json()

    # Extract customizable parameters from request
    voice_url = data.get("voice_url", ADAPTERS_URL + "/twilio/call")
    sms_url = data.get("sms_url", ADAPTERS_URL + "/twilio/sms")
    status_callback = data.get("status_callback", ADAPTERS_URL + "/twilio/call-status")
    phone_country = data.get("phone_country", "US")

    # Additional args for phone_country
    additional_args = {}
    if phone_country == "GB":
        additional_args["bundle_sid"] = "BU92b4971def01df8ce390153e23645323"
    elif phone_country in ["NL", "FI"]:
        additional_args["address_sid"] = "AD742b83eb0aab7a249e7a3f2f5fb615c0"
    elif phone_country == "AU":
        additional_args["bundle_sid"] = "BUd8f2d4e2fe905d85653f738d7323c88b"
        additional_args["address_sid"] = "AD828c09f385dea4f977464da90006bfd7"
    elif phone_country == "TH":
        additional_args["bundle_sid"] = "BUadbcfca4db22f76c6840ced254c10a11"
        additional_args["address_sid"] = "ADdf839edff37d001d2634edc9b0c4a304"
    elif phone_country == "PL":
        additional_args["bundle_sid"] = "BU0864466d980ebd9df91768d9123110b2"
        additional_args["address_sid"] = "ADdf839edff37d001d2634edc9b0c4a304"

    # Initialize Twilio client
    twilio_client = get_twilio_client()

    # Search for available mobile number
    numbers = []
    try:
        numbers += twilio_client.available_phone_numbers(phone_country).local.list(
            limit=1,
            sms_enabled=True,
            voice_enabled=True,
            beta=False,
        )
    except Exception as e:
        pass

    try:
        numbers += twilio_client.available_phone_numbers(phone_country).mobile.list(
            limit=1,
            sms_enabled=True,
            voice_enabled=True,
            beta=False,
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
        status_callback=status_callback,
        status_callback_method="POST",
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
        phone_number=phone_number,
        limit=1,
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
                DeleteSIPTrunkRequest(sip_trunk_id=item.sip_trunk_id),
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
        friendly_name=conference_name,
        status="in-progress",
    )
    conference = (
        twilio_client.conferences(conferences[0].sid).participants(call_sid).delete()
    )
    return Response(status_code=200)


@auth_router.post("/end-conference")
async def end_conference(request: Request):
    data = await request.json()
    conference_name = data.get("ConferenceName")

    twilio_client = get_twilio_client()
    conferences = twilio_client.conferences.list(
        friendly_name=conference_name,
        status="in-progress",
    )
    conference = twilio_client.conferences(conferences[0].sid).update(
        status="completed",
    )
    return {"success": True, "status": conference.status}


@unauth_router.post("/conference-status")
async def conference_status(request: Request):
    data = await request.form()
    conference_status = data.get("StatusCallbackEvent")
    conference_sid = data.get("ConferenceSid")

    twilio_client = get_twilio_client()
    if conference_status == "end":
        # Unmute LiveKit agent (Agent A) after User B hangs up
        participants = twilio_client.conferences(conference_sid).participants.list()
        for participant in participants:
            twilio_client.conferences(conference_sid).participants(
                participant.sid,
            ).update(muted=False)
    return Response(status_code=200)


@unauth_router.post("/twiml")
async def twiml(request: Request):
    data = await request.form()
    twilio_number = data.get("From")
    phone_number = "+" + request.query_params.get("phone_number").replace(" ", "")
    call_status_url = ADAPTERS_URL + "/twilio/call-status"
    resp_user = VoiceResponse()
    dial = resp_user.dial(caller_id=twilio_number, timeout=15)
    dial.number(
        phone_number,
        status_callback_event="initiated ringing answered completed",
        status_callback=call_status_url,
    )
    print("TWIML response:", str(resp_user))
    return Response(status_code=200, content=str(resp_user), media_type="text/xml")
