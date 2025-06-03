# ---------------------------------------------------------------------------
# Cloud Function entry point
# ---------------------------------------------------------------------------

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


def get_assistant_id(
    email_id: str = None,
    phone_number: str = None,
) -> str:
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
    response = requests.get(
        "https://api.unify.ai/v0/admin/assistant",
        params=params,
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
    ).json()
    if "detail" in response:
        return "default-assistant"
    assistants = response["info"]
    if len(assistants) == 0:
        return "default-assistant"
    return assistants[0]["agent_id"]


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


@functions_framework.http
def twilio_call_webhook(request: Request):
    print("🚀 Safe Twilio webhook started")

    # get twilio number and caller number
    to_number = request.form.get("To", "")
    from_number = request.form.get("From", "")
    twilio_number = to_number or ""
    caller_number = from_number or ""
    print(f"📞 Received call from {caller_number} to {twilio_number}")

    # get assistant id from email id
    assistant_id = get_assistant_id(phone_number=to_number)

    # Create unique room name with timestamp to avoid conflicts
    room_name = f"call-{twilio_number[1:]}-{caller_number[1:]}-{int(time.time())}"
    agent_name = f"unity-{twilio_number.replace('+', '')}"

    print(f"🏠 LiveKit room: {room_name}")
    print(f"🤖 Agent name: {agent_name}")

    # Create LiveKit room and dispatch agent immediately
    livekit_success = False
    try:
        import asyncio

        # Create metadata for the agent
        agent_metadata = {
            "caller_number": caller_number,
            "twilio_number": twilio_number,
            "call_type": "inbound",
            "timestamp": int(time.time() * 1000),
        }

        # Create room and dispatch agent using LiveKit API
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            dispatch = loop.run_until_complete(
                create_room_and_dispatch_agent(
                    room_name=room_name,
                    agent_name=agent_name,
                    metadata=agent_metadata,
                )
            )
            print(f"✅ LiveKit room created and agent dispatched successfully")
            print(f"Dispatch ID: {dispatch.id}")
            livekit_success = True
        finally:
            loop.close()

    except Exception as e:
        print(f"❌ Error creating LiveKit room and dispatching agent: {str(e)}")
        livekit_success = False

    # Create TwiML response
    response = VoiceResponse()

    # Try SIP connection if LiveKit setup succeeded, otherwise fallback
    if livekit_success and os.getenv("LIVEKIT_SIP_URI"):
        try:
            print("🔄 Attempting SIP connection to LiveKit")

            # Create dial with timeout
            dial = response.dial(timeout=30)

            # Simple SIP URI without complex authentication first
            sip_uri = f"sip:{room_name}@{os.getenv('LIVEKIT_SIP_URI')}"
            print(f"📞 Connecting to SIP URI: {sip_uri}")

            # Try simple SIP connection first
            dial.sip(sip_uri)

            print(f"📋 Generated TwiML with SIP: {response}")

        except Exception as e:
            print(f"❌ SIP connection failed: {str(e)}")
            # Fall back to simple response
            response = VoiceResponse()
            response.say(
                "Hello! I'm your AI assistant. I'm having trouble connecting right now, but I'm here to help."
            )

    else:
        print("⚠️ Falling back to simple response")
        response.say(
            "Hello! I'm your AI assistant. Please hold while I get ready to help you."
        )

        # Add a brief pause then try to connect agent in background
        response.pause(length=2)
        response.say("How can I help you today?")

    # Publish to pubsub for monitoring
    try:
        pubsub_client = pubsub_v1.PublisherClient()
        topic_path = pubsub_client.topic_path(
            os.getenv("PROJECT_ID"), f"unity-{assistant_id}"
        )
        print(f"📨 Publishing call to Pub/Sub at path: {topic_path}")

        pubsub_message = {
            "thread": "call",
            "event": {
                "room_name": room_name,
                "caller_number": caller_number,
                "twilio_number": twilio_number,
                "agent_name": agent_name,
                "livekit_success": livekit_success,
                "sip_attempted": bool(os.getenv("LIVEKIT_SIP_URI")),
                "call_method": "safe_fallback",
                "timestamp": int(time.time() * 1000),
            },
        }
        pubsub_client.publish(
            topic_path,
            json.dumps(pubsub_message).encode("utf-8"),
        )
        print("✅ Call published to Pub/Sub successfully")
    except Exception as e:
        print(f"⚠️ Error publishing to Pub/Sub: {str(e)}")

    print("🎯 Returning TwiML response")
    print(f"Final TwiML: {response}")
    return Response(response=str(response), mimetype="text/xml")


@functions_framework.http
def twilio_msg_webhook(request: Request):
    print("twilio_msg_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""
    print(f"Received message from {from_number} to {to_number} with body: {body}")

    # get assistant id from email id
    assistant_id = get_assistant_id(phone_number=to_number)

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
