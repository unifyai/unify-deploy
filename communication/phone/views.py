import os
import json
from urllib.parse import quote_plus
from fastapi import APIRouter, Response, Request, HTTPException
from twilio.twiml.voice_response import VoiceResponse
from google.cloud import pubsub_v1
from livekit.api import (
    LiveKitAPI,
    SIPInboundTrunkInfo,
    CreateSIPInboundTrunkRequest,
    CreateAgentDispatchRequest,
    RoomCompositeEgressRequest,
    EncodedFileOutput,
    GCPUpload,
    WebhookConfig,
    TokenVerifier,
    WebhookReceiver,
)
from livekit.protocol.sip import (
    ListSIPInboundTrunkRequest,
    DeleteSIPTrunkRequest,
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


def _publish_recording_ready(
    assistant_id: str,
    conference_name: str,
    recording_url: str,
):
    """Publish a recording_ready event to the assistant's Pub/Sub topic."""
    is_staging = bool(os.getenv("STAGING"))
    topic_name = f"unity-{assistant_id}" + ("" if not is_staging else "-staging")
    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)
    message = {
        "thread": "recording_ready",
        "event": {
            "assistant_id": str(assistant_id),
            "conference_name": conference_name,
            "recording_url": recording_url,
        },
    }
    publish_future = publisher.publish(
        topic_path,
        json.dumps(message).encode("utf-8"),
    )
    if "test" in str(assistant_id):
        publish_future.result(timeout=10)
    print(
        f"[Recording] Published recording_ready for assistant {assistant_id}, "
        f"conference_name={conference_name}",
    )


# Endpoints - Form format
@unauth_router.post("/egress-complete")
async def egress_complete(request: Request):
    """Handle LiveKit Egress completion webhooks.

    LiveKit Egress already uploaded the recording to GCS. We just verify
    the webhook signature, construct the public URL, and publish a
    recording_ready Pub/Sub event so Unity stores the URL on the exchange.
    """
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    api_secret = os.getenv("LIVEKIT_API_SECRET", "")
    auth_token = request.headers.get("Authorization", "")

    body = (await request.body()).decode()
    try:
        receiver = WebhookReceiver(
            TokenVerifier(api_key=api_key, api_secret=api_secret),
        )
        event = receiver.receive(body, auth_token)
    except Exception as e:
        print(f"[Egress] Webhook verification failed: {e}")
        return Response(status_code=401)

    if event.event != "egress_ended":
        return Response(status_code=200)

    egress_info = event.egress_info
    print(
        f"[Egress] Egress {egress_info.egress_id} ended for room "
        f"'{egress_info.room_name}' status={egress_info.status}",
    )

    if not egress_info.file_results:
        print("[Egress] No file results in egress info, skipping")
        return Response(status_code=200)

    file_result = egress_info.file_results[0]
    gcs_bucket = os.getenv(
        "LIVEKIT_EGRESS_GCS_BUCKET",
        "assistant-call-recordings",
    )
    recording_url = (
        f"https://storage.googleapis.com/{gcs_bucket}/{file_result.filename}"
    )

    assistant_id = request.query_params.get("assistant_id", "")
    room_name = request.query_params.get("room_name", egress_info.room_name)

    if not assistant_id:
        print("[Egress] Missing assistant_id in webhook params, cannot publish event")
        return Response(status_code=200)

    _publish_recording_ready(
        assistant_id=assistant_id,
        conference_name=room_name,
        recording_url=recording_url,
    )

    print(
        f"[Egress] Recording ready for assistant {assistant_id}, "
        f"room '{room_name}', size={file_result.size} bytes",
    )
    return {"success": True}


def get_livekit_api():
    """Get LiveKit API client"""
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not url or not api_key or not api_secret:
        raise RuntimeError(
            "LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET must be set",
        )

    return LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)


async def create_room_and_dispatch_livekit_agent(
    room_name: str,
    livekit_agent_name: str,
    metadata: dict = None,
    *,
    record: bool = False,
    assistant_id: str = "",
    user_id: str = "",
):
    """Create a LiveKit room and dispatch a LiveKit agent to it.

    When *record* is True, a Room Composite Egress (audio-only, MP3) is
    started for the room.  The recording is uploaded to GCS by LiveKit and a
    completion webhook notifies this service so that it can register the
    recording in Orchestra.
    """
    livekit_api = get_livekit_api()

    try:
        # Create dispatch request - this will create the room if it doesn't exist
        dispatch_request = CreateAgentDispatchRequest(
            agent_name=livekit_agent_name,
            room=room_name,
            metadata=json.dumps(metadata) if metadata else None,
        )

        # Dispatch agent to room (creates room automatically if needed)
        dispatch = await livekit_api.agent_dispatch.create_dispatch(dispatch_request)
        print(
            f"Successfully created room '{room_name}' and dispatched LiveKit agent '{livekit_agent_name}'",
        )
        print(f"Dispatch ID: {dispatch.id}")

        if record:
            await _start_room_egress(
                livekit_api,
                room_name,
                assistant_id=assistant_id,
                user_id=user_id,
            )

        return dispatch
    except Exception as e:
        print(f"Error creating room and dispatching LiveKit agent: {str(e)}")
        raise
    finally:
        await livekit_api.aclose()


async def _start_room_egress(
    livekit_api: LiveKitAPI,
    room_name: str,
    *,
    assistant_id: str,
    user_id: str,
):
    """Start an audio-only Room Composite Egress that writes MP3 to GCS."""
    gcs_credentials = os.getenv("LIVEKIT_EGRESS_GCS_CREDENTIALS", "")
    gcs_bucket = os.getenv(
        "LIVEKIT_EGRESS_GCS_BUCKET",
        "assistant-call-recordings",
    )
    comms_url = os.getenv("UNITY_COMMS_URL", "")
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    is_staging = bool(os.getenv("STAGING"))

    prefix = "staging" if is_staging else "production"
    filepath = f"{prefix}/{assistant_id}/{room_name}.mp3"

    webhook_url = (
        f"{comms_url}/phone/egress-complete"
        f"?assistant_id={quote_plus(str(assistant_id))}"
        f"&user_id={quote_plus(str(user_id))}"
        f"&room_name={quote_plus(room_name)}"
    )

    egress_request = RoomCompositeEgressRequest(
        room_name=room_name,
        audio_only=True,
        file_outputs=[
            EncodedFileOutput(
                file_type=3,  # MP3
                filepath=filepath,
                gcp=GCPUpload(
                    credentials=gcs_credentials,
                    bucket=gcs_bucket,
                ),
            ),
        ],
        webhooks=[
            WebhookConfig(url=webhook_url, signing_key=api_key),
        ],
    )
    info = await livekit_api.egress.start_room_composite_egress(egress_request)
    print(
        f"[Egress] Started room composite egress {info.egress_id} "
        f"for room '{room_name}' -> gs://{gcs_bucket}/{filepath}",
    )


# Endpoints - JSON format
@auth_router.post("/dispatch-livekit-agent")
async def dispatch_livekit_agent(request: Request):
    data = await request.json()
    livekit_agent_name = data.get("livekit_agent_name")
    room_name = data.get("room_name")
    if not room_name:
        room_name = livekit_agent_name
    await create_room_and_dispatch_livekit_agent(
        room_name,
        livekit_agent_name,
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
    sip_uri = f"sip:+{twilio_number[1:]}@{os.getenv('LIVEKIT_SIP_URI')}"
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
