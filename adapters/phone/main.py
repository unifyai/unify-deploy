# ---------------------------------------------------------------------------
# Cloud Function entry point
# ---------------------------------------------------------------------------

import asyncio
import json
from flask import Request, Response
import functions_framework
from google.cloud import pubsub_v1
import os
import requests
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse
from twilio.twiml.messaging_response import MessagingResponse
from livekit import api
import time


def get_assistant(email_id: str = None, phone_number: str = None) -> dict[str, str]:
    """
    Get the assistant id from the email id or phone number.

    Args:
        email_id: The email id of the assistant.
        phone_number: The phone number of the assistant.

    Returns:
        The assistant id.
    """
    params = dict()
    if email_id:
        params["email"] = email_id
    if phone_number:
        params["phone"] = phone_number

    default_assistant_data = {
        "assistant_id": "default-assistant",
        "tts_provider": "cartesia",
        "voice_id": None,
        "api_key": "",
        "user_name": "",
        "user_number": "",
        "assistant_number": "",
        # "user_phone_number": "",
    }
    if "+15550100002" in phone_number:
        return default_assistant_data
    if "+15550100001" in phone_number:
        return {**default_assistant_data, "assistant_id": "default-assistant-2"}
    if "+15550100005" in phone_number:
        return {
            **default_assistant_data,
            "api_key": "",
            "assistant_id": "default-assistant-3",
            "user_name": "Ved",
            "user_number": "+15550100004",
            "assistant_number": "+15550100005",
            # "user_phone_number": "+15550100004",
        }

    response = requests.get(
        "https://api.unify.ai/v0/admin/assistant",
        params=params,
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
    ).json()

    if "detail" in response:
        return default_assistant_data
    assistants = response["info"]
    if len(assistants) == 0:
        return default_assistant_data

    return {
        "assistant_id": assistants[0]["agent_id"],
        "api_key": assistants[0]["api_key"],
        "user_name": f"{assistants[0]['first_name']} {assistants[0]['surname']}",
        "assistant_number": assistants[0]["phone"],
        "assistant_whatsapp_number": assistants[0]["assistant_whatsapp_number"],
        "assistant_email": assistants[0]["email"],
        "user_number": assistants[0]["user_phone"],
        "user_whatsapp_number": assistants[0]["user_whatsapp_number"],
        "tts_provider": assistants[0]["tts_provider"],
        "voice_id": assistants[0]["voice_id"],
    }


def start_unity_job(
    api_key: str,
    assistant_id: str,
    user_name: str,
    user_number: str,
    assistant_number: str,
    user_phone_number: str,
):
    """
    Start the service if it is not running.

    Args:
        api_key: The API key for the assistant.
        assistant_id: The ID of the assistant.
        user_name: The name of the user.
        user_number: The phone number of the user.
        assistant_number: The phone number of the assistant.
        user_phone_number: The phone number of the user.
    """
    # default option when api key isn't set
    if user_name == "":
        return

    comms_url = "https://unity-comms-app-000000000000.us-central1.run.app"

    # get commit hash
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = requests.get(
        f"{comms_url}/infra/image",
        headers=headers,
    )
    if response.status_code != 200:
        print(f"Failed to get commit hash for assistant {assistant_id}")
        return
    commit_hash = response.json()["commit_hash"]
    image = (
        "us-central1-docker.pkg.dev/gcp-project-runtime"
        f"/unity/unity:{commit_hash}"
    )

    # create job
    response = requests.post(
        f"{comms_url}/infra/job/create",
        headers=headers,
        data={
            "api_key": api_key,
            "assistant_id": assistant_id,
            "user_name": user_name,
            "user_number": user_number,
            "assistant_number": assistant_number,
            "user_phone_number": user_phone_number,
            "image": image,
        },
    )
    if response.status_code != 200:
        print(f"Failed to create job for assistant {assistant_id}")


def get_twilio_client():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)


def get_livekit_api():
    """Get LiveKit API client"""
    url = os.getenv("LIVEKIT_URL")
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not url or not api_key or not api_secret:
        raise RuntimeError(
            "LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET must be set"
        )

    return api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)


async def create_room_and_dispatch_agent(
    room_name: str, agent_name: str, metadata: dict = None
):
    """Create a LiveKit room and dispatch an agent to it"""
    livekit_api = get_livekit_api()

    try:
        # Create dispatch request - this will create the room if it doesn't exist
        dispatch_request = api.CreateAgentDispatchRequest(
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


@functions_framework.http
def twilio_call_webhook(request: Request):
    print("🚀 Minimal change webhook started")
    # get twilio number and caller number
    to_number = request.form.get("To", "")
    from_number = request.form.get("From", "")
    twilio_number = to_number or ""
    caller_number = from_number or ""
    print(f"Received call from {caller_number} to {twilio_number}")

    # get assistant id from email id
    assistant_data = get_assistant(phone_number=to_number)
    api_key = assistant_data["api_key"]
    assistant_id = assistant_data["assistant_id"]
    user_name = assistant_data["user_name"]
    user_number = assistant_data["user_number"]
    assistant_number = assistant_data["assistant_number"]
    # user_phone_number = assistant_data["user_phone_number"]
    tts_provider = assistant_data["tts_provider"]
    voice_id = assistant_data["voice_id"]

    # start unity job
    start_unity_job(
        api_key,
        assistant_id,
        user_name,
        user_number,
        assistant_number,
        user_number,  # user_phone_number,
    )

    # FIXED: Create conference name and sip uri with unique timestamp
    conference_name = f"Unity_{twilio_number[1:]}"
    room_name = f"unity_{twilio_number}"  # Consistent room per assistant
    sip_uri = f"sip:+{twilio_number[1:]}@{os.getenv('LIVEKIT_SIP_URI')}"
    print(f"Setting up conference {conference_name} with SIP URI {sip_uri}")
    print(f"LiveKit room will be: {room_name}")

    # publish to pubsub - let the pubsub handler dispatch the agent when worker is ready
    pubsub_client = pubsub_v1.PublisherClient()
    topic_path = pubsub_client.topic_path(
        os.getenv("PROJECT_ID"), f"unity-{assistant_id}"
    )
    print(f"Publishing call to Pub/Sub at path: {topic_path}")
    try:
        pubsub_message = {
            "thread": "call",
            "event": {
                "conference_name": conference_name,
                "caller_number": caller_number,
                "sip_uri": sip_uri,
                "livekit_room": room_name,  # Include LiveKit room name
                "assistant_id": assistant_id,  # Include for agent dispatch
                "tts_provider": tts_provider,
                "voice_id": voice_id,  # Include for agent dispatch
                "action": "start_worker",  # Signal that worker should start (agent already dispatched)
                "timestamp": int(time.time() * 1000),  # For timing analysis
                "call_metadata": {
                    "twilio_number": twilio_number,
                    "call_type": "inbound",
                    "room_created": True,  # Confirms room was created
                    "bridge_established": True,  # Confirms SIP bridge is ready
                },
            },
        }
        pubsub_client.publish(
            topic_path,
            json.dumps(pubsub_message).encode("utf-8"),
        )
        print("Call published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")

    # Create LiveKit room and dispatch agent immediately
    try:
        # Create metadata for the agent
        agent_metadata = {
            "caller_number": caller_number,
            "twilio_number": twilio_number,
            "conference_name": conference_name,
            "call_type": "inbound",
            "call_sid": None,  # Will be updated after conference setup
            "timestamp": int(time.time() * 1000),
        }

        # Create room and dispatch agent using LiveKit API
        # Agent dispatch will queue until worker comes online
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            dispatch = loop.run_until_complete(
                create_room_and_dispatch_agent(
                    room_name=room_name,
                    agent_name=room_name,
                    metadata=agent_metadata,
                )
            )
            print(f"LiveKit room created and agent dispatched successfully")
            print(
                f"Agent will join when worker comes online. Dispatch ID: {dispatch.id}"
            )
        finally:
            loop.close()

    except Exception as e:
        print(f"Error creating LiveKit room and dispatching agent: {str(e)}")
        # Continue with Twilio conference setup even if LiveKit dispatch fails

    # UNCHANGED: Keep the original conference setup (this works)
    try:
        resp_user = create_conference_response(conference_name)
        print(f"Conference response: {resp_user.to_xml()}")
        if resp_user:
            print("Conference response created successfully")
        else:
            print("Error: Failed to create conference response")
            return Response(response="Error creating conference", status=500)

        call_sid = add_user_to_conference(conference_name, caller_number, sip_uri)
        if call_sid:
            print(f"User added to conference successfully. Call SID: {call_sid}")
        else:
            print("Error: Failed to add user to conference")
            return Response(response="Error adding user to conference", status=500)

        print("Conference setup completed")
    except Exception as e:
        print(f"Error during conference setup: {str(e)}")
        return Response(response="Error setting up conference", status=500)

    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")


@functions_framework.http
def twilio_msg_webhook(request: Request):
    print("twilio_msg_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""
    print(f"Received message from {from_number} to {to_number} with body: {body}")

    # get assistant id from email id
    assistant_data = get_assistant(phone_number=to_number)
    api_key = assistant_data["api_key"]
    assistant_id = assistant_data["assistant_id"]
    user_name = assistant_data["user_name"]
    user_number = assistant_data["user_number"]
    assistant_number = assistant_data["assistant_number"]
    user_phone_number = assistant_data["user_phone_number"]

    # start unity job
    start_unity_job(
        api_key,
        assistant_id,
        user_name,
        user_number,
        assistant_number,
        user_phone_number,
    )

    # set up conference
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_path = pubsub_client.topic_path(
        os.getenv("PROJECT_ID"), f"unity-{assistant_id}"
    )
    print(f"Publishing message to Pub/Sub at path: {topic_path}")
    try:
        pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "msg",
                    "event": {
                        "to_number": to_number,
                        "from_number": from_number,
                        "body": body,
                    },
                }
            ).encode("utf-8"),
        )
        print("Message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")
    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")
