import json
import base64
import traceback
import time
from dotenv import load_dotenv
import os
import requests
from datetime import datetime, timedelta
from typing import Optional
from fastapi import BackgroundTasks, FastAPI, Request, Response
from pydantic import BaseModel

from google.cloud import pubsub_v1
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

from .helpers import (
    add_user_to_conference,
    build_webhook_context,
    create_conference_response,
    defer_to_background,
    dispatch_agent,
    get_assistant,
    get_graph_client,
    get_outlook_thread_id,
    get_thread_id,
    is_job_running,
    publish_gmail_thread_id,
    STAGING,
    ORCHESTRA_URL,
    COMMS_URL,
)

load_dotenv()
app = FastAPI(
    title="Unity Adapters",
    description="Webhook adapters for Twilio, Gmail, and internal services",
    version="1.0.0",
)


# =============================================================================
# Health Check
# =============================================================================


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


# =============================================================================
# Twilio Webhooks
# =============================================================================


@app.post("/twilio/call")
async def twilio_call_webhook(request: Request):
    """Phone call webhook endpoint - handles incoming Twilio voice calls."""
    print("twilio_call_webhook function started")
    form_data = await request.form()

    # get twilio number and caller number
    to_number = form_data.get("To", "")
    from_number = form_data.get("From", "")
    twilio_number = to_number or ""
    caller_number = from_number or ""
    print(f"Received call from {caller_number} to {twilio_number}")

    # shared context
    context = build_webhook_context("phone", to_number, from_number)
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]

    if not context["is_valid_contact"]:
        resp_user = VoiceResponse()
        resp_user.say(
            "This number is no longer active. Please visit "
            "console.unify.ai to view your assistant details."
        )
        return Response(content=str(resp_user), media_type="text/xml")

    running = context["is_job_running"]
    print(f"Job running: {running}")

    # conference name and SIP URI
    date_time = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    conference_name = f"Unity_{twilio_number[1:]}_{date_time}"
    room_name = f"unity_{twilio_number}"
    sip_uri = f"sip:+{twilio_number[1:]}@{os.getenv('LIVEKIT_SIP_URI')}"
    print(f"Setting up conference {conference_name} with SIP URI {sip_uri}")
    print(f"LiveKit room will be: {room_name}")

    # publish to Pub/Sub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    print(f"Publishing call to Pub/Sub at path: {topic_path}")
    try:
        pubsub_message = {
            "thread": "call",
            "event": {
                "contacts": contacts,
                "conference_name": conference_name,
                "caller_number": caller_number,
                "sip_uri": sip_uri,
                "livekit_room": room_name,  # Include LiveKit room name
                "assistant_id": assistant_id,  # Include for agent dispatch
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
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(pubsub_message).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("Call published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    # Conference setup
    try:
        resp_user = create_conference_response(conference_name)
        print(f"Conference response: {resp_user.to_xml()}")
        if resp_user:
            print("Conference response created successfully")
        else:
            print("Error: Failed to create conference response")
            return Response(content="Error creating conference", status_code=500)

        call_sid = add_user_to_conference(conference_name, caller_number, sip_uri)
        if call_sid:
            print(f"User added to conference successfully. Call SID: {call_sid}")
        else:
            print("Error: Failed to add user to conference")
            return Response(content="Error adding user to conference", status_code=500)

        print(f"Assistant ID: {assistant_id}")
        if assistant_id == "default-assistant":
            print(f"Dispatching agent {room_name}")
            dispatch_agent(room_name)

        print("Conference setup completed")
    except Exception as e:
        print(f"Error during conference setup: {str(e)}")
        return Response(content="Error setting up conference", status_code=500)

    print("Returning TwiML response")
    return Response(content=str(resp_user), media_type="text/xml")


@app.post("/twilio/call-status")
async def twilio_call_status_webhook(request: Request):
    """Phone call status webhook - handles Twilio call status updates."""
    form_data = await request.form()
    call_status = form_data.get("CallStatus")
    assistant_number = form_data.get("From")
    user_number = form_data.get("To")
    print(f"twilio_call_status_webhook function started: {call_status}")
    print(f"User {user_number} called by {assistant_number}")

    if call_status == "in-progress":
        # get assistant data
        context = build_webhook_context(
            "phone", assistant_number, user_number, validate_contact=False
        )
        assistant_id = context["assistant"]["assistant_id"]
        contacts = context["contacts"]

        # publish to pubsub
        pubsub_client = pubsub_v1.PublisherClient()
        topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
        topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
        print(f"Publishing call to Pub/Sub at path: {topic_path}")
        try:
            publish_future = pubsub_client.publish(
                topic_path,
                json.dumps(
                    {
                        "thread": "call_answered",
                        "event": {
                            "contacts": contacts,
                            "assistant_id": assistant_id,
                            "user_number": user_number,
                            "assistant_number": assistant_number,
                            "timestamp": int(time.time() * 1000),
                        },
                    }
                ).encode("utf-8"),
            )
            if "test" in assistant_id:
                status_id = publish_future.result(timeout=10)
                print(f"Message ID: {status_id}")
            print("Call published to Pub/Sub successfully")
        except Exception as e:
            print(f"Error publishing to Pub/Sub: {str(e)}")

    return Response(status_code=200)


@app.post("/twilio/msg")
async def twilio_msg_webhook(request: Request):
    """SMS webhook endpoint - handles incoming Twilio SMS messages."""
    print("twilio_msg_webhook function started")
    form_data = await request.form()

    # get twilio number and caller number
    to_number = form_data.get("To", "") or ""
    from_number = form_data.get("From", "") or ""
    body = form_data.get("Body", "") or ""
    print(f"Received message from {from_number} to {to_number} with body: {body}")

    # shared context
    context = build_webhook_context("msg", to_number, from_number)
    assistant_data = context["assistant"]
    assistant_id = assistant_data["assistant_id"]
    contacts = context["contacts"]

    if not context["is_valid_contact"]:
        resp_user = MessagingResponse()
        resp_user.message(
            "This number is no longer active. Please visit "
            "console.unify.ai to view your assistant details."
        )
        return Response(content=str(resp_user), media_type="text/xml")

    running = context["is_job_running"]
    print(f"Job running: {running}")

    # set up response
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    print(f"Publishing message to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "msg",
                    "event": {
                        "contacts": contacts,
                        "to_number": to_number,
                        "from_number": from_number,
                        "body": body,
                    },
                }
            ).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("Message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    print("Returning TwiML response")
    return Response(content=str(resp_user), media_type="text/xml")


@app.post("/twilio/whatsapp")
async def twilio_whatsapp_webhook(request: Request):
    """WhatsApp webhook endpoint - handles incoming Twilio WhatsApp messages."""
    print("twilio_whatsapp_webhook function started")
    form_data = await request.form()

    # get twilio number and caller number
    to_number = form_data.get("To", "") or ""
    from_number = form_data.get("From", "") or ""
    body = form_data.get("Body", "") or ""
    print(f"Received message from {from_number} to {to_number} with body: {body}")

    # shared context
    context = build_webhook_context("whatsapp", to_number, from_number)
    assistant_data = context["assistant"]
    assistant_id = assistant_data["assistant_id"]
    contacts = context["contacts"]

    if not context["is_valid_contact"]:
        resp_user = MessagingResponse()
        resp_user.message(
            "This number is no longer active. Please visit "
            "console.unify.ai to view your assistant details."
        )
        return Response(content=str(resp_user), media_type="text/xml")

    running = context["is_job_running"]
    print(f"Job running: {running}")

    # set up response
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    print(f"Publishing message to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "whatsapp",
                    "event": {
                        "contacts": contacts,
                        "to_number": to_number,
                        "from_number": from_number,
                        "body": body,
                    },
                }
            ).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("Message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing to Pub/Sub: {str(e)}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    print("Returning TwiML response")
    return Response(content=str(resp_user), media_type="text/xml")


# =============================================================================
# Unify Webhooks
# =============================================================================


class UnifyMessagePayload(BaseModel):
    assistant_id: str
    contact_id: Optional[int] = 1
    body: Optional[str] = ""


@app.post("/unify/message")
async def unify_message_webhook(request: Request):
    """Unify message webhook - handles internal message events."""
    print("unify_message_webhook function started")

    # optional auth via admin key
    shared_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    auth_header = request.headers.get("Authorization", "")
    if shared_key and auth_header != f"Bearer {shared_key}":
        print("Unauthorized unify_message request")
        return Response(status_code=401)

    # accept JSON or form payloads
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = await request.json()
        assistant_id_input = payload.get("assistant_id", "")
        contact_id = payload.get("contact_id", 1)
        body = payload.get("body", "") or ""
    else:
        form_data = await request.form()
        assistant_id_input = form_data.get("assistant_id", "")
        contact_id = form_data.get("contact_id", 1)
        body = form_data.get("Body", "") or ""

    if not assistant_id_input:
        print("Assistant ID is required")
        return Response(status_code=400)

    print(
        f"Received unify_message message for assistant_id={assistant_id_input} with body: {body}"
    )

    # shared context
    context = build_webhook_context(
        channel="unify_message",
        destination="",
        sender="",
        assistant_id=assistant_id_input,
        validate_contact=False,
        ensure_job=True,
    )
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]
    running = context["is_job_running"]
    print(f"Job running: {running}")

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    print(f"Publishing unify_message to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "unify_message",
                    "event": {
                        "contact_id": contact_id,
                        "contacts": contacts,
                        "assistant_id": assistant_id,
                        "body": body,
                    },
                }
            ).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("unify_message message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing unify_message to Pub/Sub: {str(e)}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    return Response(status_code=200)


@app.post("/unify/call")
async def unify_call_webhook(request: Request):
    """Unify call webhook - handles internal call events."""
    print("unify_call_webhook function started")

    # optional auth via admin key
    shared_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    auth_header = request.headers.get("Authorization", "")
    if shared_key and auth_header != f"Bearer {shared_key}":
        print("Unauthorized unify_call request")
        return Response(status_code=401)

    # accept JSON or form payloads
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form_data = await request.form()
        payload = dict(form_data)

    agent_name = payload.get("agent_name", "")
    room_name = payload.get("room_name", "")
    if not agent_name or not room_name:
        print("agent_name and room_name are required")
        return Response(status_code=400)

    assistant_id_input = payload.get("assistant_id", "")
    if not assistant_id_input:
        print("assistant_id is required")
        return Response(status_code=400)

    print(
        f"Received unify_call for assistant_id={assistant_id_input} room={room_name} agent_name={agent_name}"
    )

    # shared context
    context = build_webhook_context(
        channel="unify_call",
        destination="",
        sender="",
        assistant_id=assistant_id_input,
        validate_contact=False,
        ensure_job=True,
    )
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]
    running = context["is_job_running"]
    print(f"Job running: {running}")

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    print(f"Publishing unify_call to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "unify_call",
                    "event": {
                        "contacts": contacts,
                        "assistant_id": assistant_id,
                        "livekit_room": room_name,
                        "agent_name": agent_name,
                        "timestamp": int(time.time() * 1000),
                    },
                }
            ).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("unify_call message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing unify_call to Pub/Sub: {str(e)}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    return Response(status_code=200)


# =============================================================================
# Unity System Webhooks
# =============================================================================


@app.post("/unity/system-event")
async def unity_system_event_webhook(request: Request):
    """Unity system event webhook - handles system-level events."""
    print("unity_system_event_webhook function started")

    # optional auth via admin key
    shared_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    auth_header = request.headers.get("Authorization", "")
    if shared_key and auth_header != f"Bearer {shared_key}":
        print("Unauthorized unity_system_event request")
        return Response(status_code=401)

    # accept JSON or form payloads
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form_data = await request.form()
        payload = dict(form_data)

    assistant_id = payload.get("assistant_id", "")
    if not assistant_id:
        print("assistant_id is required")
        return Response(status_code=400)

    event_type = payload.get("event_type", "")
    if not event_type:
        print("event_type is required")
        return Response(status_code=400)

    message = payload.get("message", "")
    if not message:
        print("message is required")
        return Response(status_code=400)

    print(
        f"Received unity_system_event for event_type={event_type} with message={message}"
    )

    # shared context
    context = build_webhook_context(
        channel="unity_system_event",
        destination="",
        sender="",
        assistant_id=assistant_id,
        validate_contact=False,
        ensure_job=True,
    )
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]
    running = context["is_job_running"]
    print(f"Job running: {running}")

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    print(f"Publishing unity_system_event to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "unity_system_event",
                    "event": {
                        "contacts": contacts,
                        "assistant_id": assistant_id,
                        "event_type": event_type,
                        "message": message,
                    },
                }
            ).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("unity_system_event message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing unity_system_event to Pub/Sub: {str(e)}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    return Response(status_code=200)


@app.post("/log-pre-hire-chats")
async def log_pre_hire_chats_webhook(request: Request):
    """Log pre-hire chats webhook - logs chat history before hiring."""
    print("log_pre_hire_chats_webhook function started")

    # optional auth via admin key
    shared_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    auth_header = request.headers.get("Authorization", "")
    if shared_key and auth_header != f"Bearer {shared_key}":
        print("Unauthorized log_pre_hire_chats request")
        return Response(status_code=401)

    # accept JSON or form payloads
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form_data = await request.form()
        payload = dict(form_data)

    assistant_id_input = payload.get("assistant_id", "")
    if not assistant_id_input:
        print("Assistant ID is required")
        return Response(status_code=400)

    # accept list of role/msg pairs under `body`
    raw_body = payload.get("body")
    if raw_body is None:
        raw_body = payload.get("Body", "") or ""

    # If body is a JSON string, parse it; otherwise, use as-is
    try:
        body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
    except Exception:
        body = None

    # validate body: must be list of dicts with role and msg (both strings)
    if not isinstance(body, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("role"), str)
        and isinstance(item.get("msg"), str)
        for item in body
    ):
        print("Invalid body format; expected list of {role, msg} objects")
        return Response(
            content=json.dumps({"error": "body must be a list of {role, msg}"}),
            status_code=400,
            media_type="application/json",
        )

    print(
        f"Received log_pre_hire_chats for assistant_id={assistant_id_input} with {len(body)} messages"
    )

    # shared context
    context = build_webhook_context(
        channel="unify_message",
        destination="",
        sender="",
        assistant_id=assistant_id_input,
        validate_contact=False,
        ensure_job=True,
    )
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]
    running = context["is_job_running"]
    print(f"Job running: {running}")

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)
    print(f"Publishing log_pre_hire_chats to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "log_pre_hire_chats",
                    "event": {
                        "contacts": contacts,
                        "assistant_id": assistant_id,
                        "body": body,
                    },
                }
            ).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("log_pre_hire_chats message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing log_pre_hire_chats to Pub/Sub: {str(e)}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    return Response(status_code=200)


# =============================================================================
# Assistant Webhooks
# =============================================================================


@app.post("/assistant/wakeup")
async def assistant_wakeup_webhook(request: Request):
    """Assistant wakeup webhook - wakes up an assistant."""
    print("assistant_wakeup_webhook function started")
    form_data = await request.form()
    assistant_id = form_data.get("assistant_id")
    print(f"Assistant {assistant_id} woke up")

    # shared context
    build_webhook_context(
        channel="wakeup",
        destination="",
        sender="",
        assistant_id=assistant_id,
        validate_contact=False,
        ensure_job=True,
        force_start=True,
    )

    return Response(status_code=200)


@app.post("/assistant/update")
async def assistant_update_webhook(request: Request):
    """
    Webhook that receives an assistant id and publishes assistant details
    to the assistant_update topic if there is a job running.
    """
    print("assistant_update_webhook function started")

    try:
        form_data = await request.form()
        assistant_id = form_data.get("assistant_id")
        print(f"Received assistant_id: {assistant_id}")

        assistant_data = get_assistant(assistant_id=assistant_id)
        user_id = assistant_data["user_id"]
        assistant_first_name = assistant_data["assistant_first_name"]
        assistant_surname = assistant_data["assistant_surname"]
        assistant_data["assistant_name"] = f"{assistant_first_name} {assistant_surname}"
        assistant_data.pop("assistant_first_name")
        assistant_data.pop("assistant_surname")
        assistant_data.pop("assistant_whatsapp_number")

        # check if job is running
        is_default_assistant = (
            "default" in assistant_id
            or "Default Assistant" in assistant_data["assistant_about"]
        )
        running = is_job_running(user_id, assistant_id)
        print(f"Job running: {running}")

        if not is_default_assistant and not running:
            return Response(
                content=json.dumps(
                    {
                        "success": False,
                        "message": "No job currently running for this assistant",
                        "assistant_id": assistant_id,
                    }
                ),
                status_code=200,
                media_type="application/json",
            )
        elif running:
            print(f"Job running for assistant {assistant_id}: {running}")
        else:
            print(f"Default assistant {assistant_id} - skipping job running check")

        # Job is running, publish to assistant topic
        pubsub_client = pubsub_v1.PublisherClient()
        topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
        topic_path = pubsub_client.topic_path(os.getenv("PROJECT_ID"), topic_name)

        # Prepare message in the same format as startup event
        message_data = {"thread": "assistant_update", "event": assistant_data}

        print(f"Publishing assistant update to Pub/Sub at path: {topic_path}")
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(message_data).encode("utf-8"),
        )

        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")

        print("Assistant update published to Pub/Sub successfully")

        return Response(
            content=json.dumps(
                {
                    "success": True,
                    "message": "Assistant update published successfully",
                    "assistant_id": assistant_id,
                    "topic_path": topic_path,
                }
            ),
            status_code=200,
            media_type="application/json",
        )

    except Exception as e:
        print(f"Error in assistant_update_webhook: {str(e)}")
        traceback.print_exc()
        return Response(
            content=json.dumps({"error": str(e)}),
            status_code=500,
            media_type="application/json",
        )


# =============================================================================
# Email Webhooks
# =============================================================================


@app.post("/email/gmail")
async def gmail_notification_processor(request: Request):
    """
    Cloud Run endpoint that processes Gmail notifications via Pub/Sub push.
    Receives push messages from Pub/Sub subscription.
    """
    try:
        # Parse the Pub/Sub push message envelope
        envelope = await request.json()
        if not envelope or "message" not in envelope:
            print("Bad Request: no Pub/Sub message")
            return Response(content="Bad Request: no Pub/Sub message", status_code=400)

        pubsub_message = envelope.get("message", {})
        data = base64.b64decode(pubsub_message.get("data", "")).decode("utf-8")
        notification = json.loads(data)
        print(f"Received notification: {notification}")

        # extract Gmail notification details
        email_id = notification["emailAddress"]
        history_id = notification["historyId"]

        # get credentials
        creds_json = json.loads(os.getenv("GCP_SA_KEY"))
        scopes = [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
        ]
        gmail_creds = Credentials.from_service_account_info(
            creds_json,
            scopes=scopes,
            subject=email_id,
        )
        gmail_service = build("gmail", "v1", credentials=gmail_creds)

        # process the history and thread
        print(f"email_id: {email_id}, history_id: {history_id}")
        thread_id, message_id, last_message, gmail_message_id = get_thread_id(
            email_id, history_id, gmail_service
        )
        print(
            f"thread_id: {thread_id}, message_id: {message_id}, last_message: {last_message}"
        )
        if not thread_id:
            print(f"No new conversations found for user {email_id}")
            return Response(content="No new conversations", status_code=200)

        from_email = last_message["sender"].split("<")[1].split(">")[0]
        print(f"from_email: {from_email}")

        # shared context
        context = build_webhook_context("email", email_id, from_email)
        assistant_data = context["assistant"]
        assistant_id = assistant_data["assistant_id"]
        user_id = assistant_data["user_id"]
        contacts = context["contacts"]

        if not context["is_valid_contact"]:
            error_message = (
                "This email address is no longer active. Please visit "
                "console.unify.ai to view your assistant details."
            )
            return Response(content=error_message, status_code=500)

        running = context["is_job_running"]
        print(f"Job running: {running}")

        print(f"Successfully processed conversation for user {email_id}")
        publish_gmail_thread_id(
            assistant_id,
            user_id,
            thread_id,
            message_id,
            last_message,
            contacts,
            gmail_message_id,
        )
        return Response(content="OK", status_code=200)

    except Exception as e:
        error_message = f"Error processing notification: {str(e)}"
        traceback.print_exc()
        print(error_message)
        return Response(content=error_message, status_code=500)


@app.post("/email/outlook")
@defer_to_background
async def outlook_notification_processor(
    request: Request, background_tasks: BackgroundTasks
):
    """
    Webhook endpoint to receive Microsoft Graph change notifications.
    Processes Outlook email notifications similar to Gmail notification processor.

    Microsoft has a timeout of 3s for the webhook to return, which isn't realistic
    for processing a message with the calls to orchestra and everything.
    The @defer_to_background decorator handles returning 200 immediately and
    scheduling this function as a background task.
    """
    print(f"\n[BACKGROUND] outlook_notification_processor STARTED")
    try:
        body = await request.body()
        print(f"[BACKGROUND] Got body: {len(body)} bytes")
        json_body = json.loads(body)
        notifications = json_body.get("value", [])
        print(f"[BACKGROUND] Processing {len(notifications)} notification(s)...")

        # Expected client state for validation (set during subscription creation)
        expected_client_state = os.getenv(
            "OUTLOOK_WEBHOOK_SECRET", "unify-outlook-webhook"
        )

        for notification in notifications:
            print(f"\n[NOTIFICATION]")
            print(f"  subscriptionId: {notification.get('subscriptionId')}")
            print(f"  changeType: {notification.get('changeType')}")
            print(f"  resource: {notification.get('resource')}")
            print(f"  clientState: {notification.get('clientState')}")
            print(f"  tenantId: {notification.get('tenantId')}")

            # Validate clientState for security
            client_state = notification.get("clientState")
            if client_state != expected_client_state:
                print(f"  [WARNING] Invalid clientState received: {client_state}")
                continue

            # Parse resource path to get user email and message ID
            # Microsoft Graph uses PascalCase: Users/{id}/Messages/{id}
            resource = notification.get("resource", "")
            print(f"  resource: {resource}")
            print(f"  /Messages/ in resource: {'/Messages/' in resource}")
            if "/Messages/" not in resource:
                continue

            # Parse: Users/{user_id}/MailFolders/Inbox/Messages/{message_id}
            parts = resource.split("/")
            try:
                user_index = parts.index("Users") + 1
                email_id = parts[user_index]

                messages_index = parts.index("Messages") + 1
                outlook_message_id = parts[messages_index]

                print(f"  user: {email_id}")
                print(f"  message_id: {outlook_message_id}")
            except (ValueError, IndexError) as e:
                print(f"  [ERROR] Could not parse resource path '{resource}': {e}")
                continue

            # Get Graph client
            graph_client = get_graph_client()

            # Process the message (similar to get_thread_id for Gmail)
            print(f"\nemail_id: {email_id}, message_id: {outlook_message_id}")
            conversation_id, message_id, last_message = await get_outlook_thread_id(
                email_id, outlook_message_id, graph_client
            )
            print(
                f"conversation_id: {conversation_id}, message_id: {message_id}, last_message: {last_message}"
            )

            if not conversation_id:
                print(f"No new conversations found for user {email_id}")
                continue

            from_email = last_message["sender"]
            print(f"from_email: {from_email}")

            # Print message details
            print(f"\n[MESSAGE DETAILS]")
            print(f"  From: {last_message['sender']}")
            print(f"  To: {last_message['to']}")
            print(f"  CC: {last_message['cc']}")
            print(f"  Subject: {last_message['subject']}")
            content = last_message.get("content", "")
            print(
                f"  Body preview: {content[:200]}..."
                if len(content) > 200
                else f"  Body: {content}"
            )
            print(f"  Has attachments: {last_message.get('has_attachments', False)}")
            print(f"  Conversation ID: {conversation_id}")
            print(f"  Received: {last_message.get('received_at')}")

            # TODO: Add shared context and pub/sub integration similar to Gmail
            # context = build_webhook_context("outlook_email", email_id, from_email)
            # assistant_data = context["assistant"]
            # assistant_id = assistant_data["assistant_id"]
            # user_id = assistant_data["user_id"]
            # contacts = context["contacts"]
            #
            # if not context["is_valid_contact"]:
            #     continue
            #
            # publish_outlook_thread(assistant_id, user_id, conversation_id, message_id, last_message, contacts)

            print(
                f"\n[BACKGROUND] Successfully processed conversation for user {email_id}"
            )

    except Exception as e:
        print(f"[BACKGROUND] Error processing Outlook notifications: {str(e)}")
        traceback.print_exc()


# =============================================================================
# Scheduled Endpoints
# =============================================================================


@app.post("/scheduled/email-watch-renewer")
async def email_watch_renewer(request: Request):
    """Cloud Run endpoint that renews Gmail watches for multiple users."""
    payload = await request.json()
    test = payload.get("test", False)

    if not test:
        emails = requests.get(
            f"{ORCHESTRA_URL}/admin/assistant/emails",
            headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
        ).json()["info"]
    else:
        emails = ["default-test-assistant@unify.ai"]
    print(f"Emails to renew: {emails}")

    results = {}

    # Process each email
    print("Renewing emails...")
    for email in emails:
        try:
            admin_key = os.getenv("ORCHESTRA_ADMIN_KEY")
            results[email] = requests.post(
                f"{COMMS_URL}/gmail/watch",
                json={
                    "primary_email": email,
                    "topic_name": (
                        "gmail-notifications"
                        if not STAGING
                        else "gmail-notifications-staging"
                    ),
                },
                headers={"Authorization": f"Bearer {admin_key}"},
            ).json()
        except Exception as e:
            error_message = f"Error renewing Gmail watch for {email}: {str(e)}"
            print(error_message)
            results[email] = {"success": False, "error": error_message}
    print("Results of renewing emails:")
    print(results)

    # renew policy assistant
    if STAGING and not test:
        admin_key = os.getenv("ORCHESTRA_ADMIN_KEY")
        response = requests.post(
            f"{COMMS_URL}/gmail/watch",
            json={"primary_email": "mh-policies@unify.ai", "topic_name": "intranet"},
            headers={"Authorization": f"Bearer {admin_key}"},
        ).json()
        print("Renewed policy assistant")
        print(response)

    return results


@app.post("/scheduled/idle-job-creator")
async def idle_job_creator(request: Request):
    """Cloud Run endpoint that creates a new idle job."""
    if not STAGING:
        return Response(
            content="Production job creation is not enabled", status_code=200
        )
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = requests.get(f"{COMMS_URL}/infra/image", headers=headers)
    commit_hash = response.json()["commit_hash"]
    image = (
        "us-central1-docker.pkg.dev/gcp-project-runtime/unity"
        + ("/unity:" if not STAGING else "/unity-staging:")
        + commit_hash
    )
    response = requests.post(
        f"{COMMS_URL}/infra/job/create", data={"image": image}, headers=headers
    )
    return response.json()


@app.post("/scheduled/idle-job-cleaner")
async def idle_job_cleaner(request: Request):
    """Cloud Run endpoint that cleans idle jobs that have been around for >24 hours."""
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    idle_jobs = []

    # get all jobs
    jobs = requests.get(f"{COMMS_URL}/infra/jobs", headers=headers).json()
    job_names = [job["job_name"] for job in jobs["jobs"]]
    job_names = [
        job_name
        for job_name in job_names
        if (STAGING and "staging" in job_name)
        or (not STAGING and "staging" not in job_name)
    ]
    print(f"Job names: {job_names}")

    for job_name in job_names:
        # get logs
        logs = (
            requests.get(
                f"{COMMS_URL}/infra/job/logs",
                params={"job_name": job_name},
                headers=headers,
            )
            .json()
            .get("logs", [])
        )
        print(f"Logs: {logs}")

        # check if job is idle
        if (
            "ping received - keeping conversation manager alive" in logs
            and "Inactivity timeout reached (360s), requesting shutdown..." not in logs
            and "Graceful shutdown completed" not in logs
            and "Shutting down convo manager..." not in logs
        ):
            idle_jobs.append(job_name)

    new_idle_jobs = []
    for job_name in idle_jobs:
        # check if job is older than 10 minutes
        job_timestamp_str = job_name.replace("unity-", "").replace("-staging", "")
        job_timestamp = datetime.strptime(job_timestamp_str, "%Y-%m-%d-%H-%M-%S")
        now = datetime.now()
        delta = now - job_timestamp
        if delta < timedelta(minutes=11):
            new_idle_jobs.append(job_name)

    print(f"Idle jobs: {idle_jobs}")
    print(f"New idle jobs: {new_idle_jobs}")
    if len(new_idle_jobs) == 0:
        if len(idle_jobs) != 0:
            idle_jobs = sorted(idle_jobs)[:-1]
    else:
        new_idle_jobs = [sorted(new_idle_jobs)[-1]]
    idle_jobs = list(filter(lambda job: job not in new_idle_jobs, idle_jobs))

    # delete all old idle jobs
    for job_name in idle_jobs:
        requests.delete(
            f"{COMMS_URL}/infra/job/delete",
            data={"job_name": job_name},
            headers=headers,
        )

    return Response(content=json.dumps({"idle_jobs": idle_jobs}), status_code=200)


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    print("Starting Unity Adapters server...")
    print("Available endpoints:")
    print("  Twilio:")
    print("    - POST /twilio/call")
    print("    - POST /twilio/call-status")
    print("    - POST /twilio/msg")
    print("    - POST /twilio/whatsapp")
    print("  Unify:")
    print("    - POST /unify/message")
    print("    - POST /unify/call")
    print("  Unity:")
    print("    - POST /unity/system-event")
    print("    - POST /log-pre-hire-chats")
    print("  Assistant:")
    print("    - POST /assistant/wakeup")
    print("    - POST /assistant/update")
    print("  Email Webhooks:")
    print("    - POST /email/gmail")
    print("    - POST /email/outlook")
    print("  Scheduled:")
    print("    - POST /scheduled/email-watch-renewer")
    print("    - POST /scheduled/idle-job-creator")
    print("    - POST /scheduled/idle-job-cleaner")
    print("Server running at: http://localhost:8080")

    uvicorn.run("adapters.main:app", host="0.0.0.0", port=8080, reload=True)
