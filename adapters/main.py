import json
import base64
import traceback
import time
import functions_framework
import os
import requests
from datetime import datetime, timedelta
from flask import Request, Response

from google.cloud import pubsub_v1
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

from helpers import (
    add_user_to_conference,
    build_webhook_context,
    check_valid_contact,
    create_conference_response,
    dispatch_agent,
    get_assistant,
    get_thread_id,
    is_job_running,
    publish_thread_id,
    STAGING,
    ORCHESTRA_URL,
    COMMS_URL,
)


@functions_framework.http
def unify_message_webhook(request: Request):
    print("unify_message_webhook function started")
    # optional auth via admin key
    shared_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    auth_header = request.headers.get("Authorization", "")
    if shared_key and auth_header != f"Bearer {shared_key}":
        print("Unauthorized unify_message request")
        return Response(status=401)

    # accept JSON or form payloads
    payload = request.get_json(silent=True) or {}
    assistant_id_input = payload.get("assistant_id") or request.form.get(
        "assistant_id", ""
    )
    contact_id = payload.get("contact_id") or request.form.get("contact_id", 1)
    if not assistant_id_input:
        print(f"Assistant ID is required")
        return Response(status_code=400)
    body = payload.get("body") or request.form.get("Body", "") or ""
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
        return Response(response="Error publishing to Pub/Sub", status=500)

    return Response(status=200)


@functions_framework.http
def unify_call_webhook(request: Request):
    print("unify_call_webhook function started")
    # optional auth via admin key
    shared_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    auth_header = request.headers.get("Authorization", "")
    if shared_key and auth_header != f"Bearer {shared_key}":
        print("Unauthorized unify_call request")
        return Response(status=401)

    # accept JSON or form payloads
    payload = request.get_json(silent=True) or {}
    agent_name = payload.get("agent_name") or request.form.get("agent_name", "")
    room_name = payload.get("room_name") or request.form.get("room_name", "")
    if not agent_name or not room_name:
        print("agent_name and room_name are required")
        return Response(status=400)

    # Require assistant_id explicitly
    assistant_id_input = payload.get("assistant_id") or request.form.get(
        "assistant_id", ""
    )
    if not assistant_id_input:
        print("assistant_id is required")
        return Response(status=400)

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
        return Response(response="Error publishing to Pub/Sub", status=500)

    return Response(status=200)


@functions_framework.http
def log_pre_hire_chats_webhook(request: Request):
    print("log_pre_hire_chats_webhook function started")
    # optional auth via admin key
    shared_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    auth_header = request.headers.get("Authorization", "")
    if shared_key and auth_header != f"Bearer {shared_key}":
        print("Unauthorized log_pre_hire_chats request")
        return Response(status=401)

    # accept JSON or form payloads
    payload = request.get_json(silent=True) or {}
    assistant_id_input = payload.get("assistant_id") or request.form.get(
        "assistant_id", ""
    )
    if not assistant_id_input:
        print(f"Assistant ID is required")
        return Response(status_code=400)

    # accept list of role/msg pairs under `body`
    raw_body = payload.get("body")
    if raw_body is None:
        raw_body = request.form.get("Body", "") or ""
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
            response=json.dumps({"error": "body must be a list of {role, msg}"}),
            status=400,
            mimetype="application/json",
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
        return Response(response="Error publishing to Pub/Sub", status=500)

    return Response(status=200)


@functions_framework.http
def unity_system_event_webhook(request: Request):
    print("unity_system_event_webhook function started")
    # optional auth via admin key
    shared_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    auth_header = request.headers.get("Authorization", "")
    if shared_key and auth_header != f"Bearer {shared_key}":
        print("Unauthorized unity_system_event request")
        return Response(status=401)
    
    # accept JSON or form payloads
    payload = request.get_json(silent=True) or {}
    assistant_id = payload.get("assistant_id") or request.form.get("assistant_id") or ""
    if not assistant_id:
        print("assistant_id is required")
        return Response(status=400)

    event_type = payload.get("event_type") or request.form.get("event_type", "")
    if not event_type:
        print("event_type is required")
        return Response(status=400)
    
    message = payload.get("message") or request.form.get("message", "")
    if not message:
        print("message is required")
        return Response(status=400)
    
    print(f"Received unity_system_event for event_type={event_type} with message={message}")
    
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
        return Response(response="Error publishing to Pub/Sub", status=500)

    return Response(status=200)


@functions_framework.http
def assistant_wakeup_webhook(request: Request):
    print("assistant_wakeup_webhook function started")
    assistant_id = request.form.get("assistant_id")
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

    return Response(status=200)


@functions_framework.http
def twilio_call_status_webhook(request: Request):
    call_status = request.form.get("CallStatus")
    assistant_number = request.form.get("From")
    user_number = request.form.get("To")
    print(f"twilio_call_status_webhook function started: {call_status}")
    print(f"User {user_number} called by {assistant_number}")
    if call_status == "in-progress":
        # get assistant data
        assistant_data = get_assistant(phone_number=assistant_number)
        api_key = assistant_data["api_key"]
        assistant_id = assistant_data["assistant_id"]
        assistant_first_name = assistant_data["assistant_first_name"]
        assistant_surname = assistant_data["assistant_surname"]
        user_number = assistant_data["user_number"]
        assistant_number = assistant_data["assistant_number"]

        # check if contact is valid
        contacts = check_valid_contact(
            email_id="",
            phone_number=user_number,
            medium="phone",
            assistant_context=f"{assistant_first_name}{assistant_surname}",
            api_key=api_key,
            user_number=user_number,
        )
        if "default" not in assistant_id and not contacts:
            print(f"User {user_number} is not a valid contact")
            return Response(status_code=200)

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
                        "thread": "call_received",
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
    return Response(status=200)


# phone webhook
@functions_framework.http
def twilio_call_webhook(request: Request):
    print("twilio_call_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "")
    from_number = request.form.get("From", "")
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
        return Response(response=str(resp_user), mimetype="text/xml")

    running = context["is_job_running"]
    print(f"Job running: {running}")

    # conference name and SIP URI
    date_time = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    conference_name = f"Unity_{twilio_number[1:]}_{date_time}"
    room_name = f"unity_{twilio_number}"  # Consistent room per assistant
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
        return Response(response="Error publishing to Pub/Sub", status=500)

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

        print(f"Assistant ID: {assistant_id}")
        if assistant_id == "default-assistant":
            print(f"Dispatching agent {room_name}")
            dispatch_agent(room_name)

        print("Conference setup completed")
    except Exception as e:
        print(f"Error during conference setup: {str(e)}")
        return Response(response="Error setting up conference", status=500)

    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")


# sms webhook
@functions_framework.http
def twilio_msg_webhook(request: Request):
    print("twilio_msg_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""
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
        return Response(response=str(resp_user), mimetype="text/xml")

    running = context["is_job_running"]
    print(f"Job running: {running}")

    # set up conference
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
        return Response(response="Error publishing to Pub/Sub", status=500)
    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")


# whatsapp webhook
@functions_framework.http
def twilio_whatsapp_webhook(request: Request):
    print("twilio_whatsapp_webhook function started")
    # get twilio number and caller number
    to_number = request.form.get("To", "") or ""
    from_number = request.form.get("From", "") or ""
    body = request.form.get("Body", "") or ""
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
        return Response(response=str(resp_user), mimetype="text/xml")

    running = context["is_job_running"]
    print(f"Job running: {running}")

    # set up conference
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
        return Response(response="Error publishing to Pub/Sub", status=500)

    print("Returning TwiML response")
    return Response(response=str(resp_user), mimetype="text/xml")


# email webhook
@functions_framework.http
def email_watch_renewer(request):
    """Cloud Function that renews Gmail watches for multiple users."""
    # ToDo: make orchestra admin call to get all assistant emails
    test = request.json.get("test")
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
                f"{COMMS_URL}/email/watch",
                json={"primary_email": email},
                headers={"Authorization": f"Bearer {admin_key}"},
            ).json()
        except Exception as e:
            error_message = f"Error renewing Gmail watch for {email}: {str(e)}"
            print(error_message)
            results[email] = {"success": False, "error": error_message}
    print("Results of renewing emails:")
    print(results)

    # renew policy assistant
    if os.getenv("STAGING") and not test:
        admin_key = os.getenv("ORCHESTRA_ADMIN_KEY")
        response = requests.post(
            f"{COMMS_URL}/email/watch",
            json={"primary_email": "mh-policies@unify.ai", "topic_name": "intranet"},
            headers={"Authorization": f"Bearer {admin_key}"},
        ).json()
        print("Renewed policy assistant")
        print(response)

    return results


@functions_framework.cloud_event
def email_notification_processor(cloud_event):
    """Cloud Function triggered by Pub/Sub that processes Gmail notifications."""
    try:
        # Extract the Pub/Sub message from the cloud event
        envelope = json.loads(
            base64.b64decode(cloud_event.data["message"]["data"]).decode("utf-8")
        )
        print(f"Received notification: {envelope}")

        # Extract Gmail notification details
        email_id = envelope["emailAddress"]
        history_id = envelope["historyId"]

        # shared context
        context = build_webhook_context("email", email_id, "")
        assistant_data = context["assistant"]
        assistant_id = assistant_data["assistant_id"]
        user_id = assistant_data["user_id"]
        contacts = context["contacts"]

        if not context["is_valid_contact"]:
            error_message = (
                "This email address is no longer active. Please visit "
                "console.unify.ai to view your assistant details."
            )
            return error_message, 500
        running = context["is_job_running"]
        print(f"Job running: {running}")

        # Get credentials
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

        # Process the history and thread
        print(f"email_id: {email_id}, history_id: {history_id}")
        thread_id, message_id, last_message, gmail_message_id = get_thread_id(
            email_id, history_id, gmail_service
        )
        print(
            f"thread_id: {thread_id}, message_id: {message_id}, last_message: {last_message}"
        )

        if thread_id:
            print(f"Successfully processed conversation for user {email_id}")
            publish_thread_id(
                assistant_id,
                user_id,
                thread_id,
                message_id,
                last_message,
                contacts,
                gmail_message_id,
            )
            return "OK"
        else:
            print(f"No new conversations found for user {email_id}")
            return "No new conversations"

    except Exception as e:
        error_message = f"Error processing notification: {str(e)}"
        traceback.print_exc()
        print(error_message)
        return error_message, 500


# infra webhook
@functions_framework.http
def idle_job_creator(request):
    """Cloud Function that creates a new idle job."""
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


@functions_framework.http
def idle_job_cleaner(request):
    """Cloud Function that renews idle jobs that have been around for >24 hours.."""
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

    return Response(response=json.dumps({"idle_jobs": idle_jobs}), status=200)


@functions_framework.http
def assistant_update_webhook(request: Request):
    """
    Webhook that receives an assistant id and publishes assistant details
    to the assistant_update topic if there is a job running.
    """
    print("assistant_update_webhook function started")

    try:
        # get assistant from request
        assistant_id = request.form.get("assistant_id")
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
                response=json.dumps(
                    {
                        "success": False,
                        "message": "No job currently running for this assistant",
                        "assistant_id": assistant_id,
                    }
                ),
                status=200,
                mimetype="application/json",
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
            response=json.dumps(
                {
                    "success": True,
                    "message": "Assistant update published successfully",
                    "assistant_id": assistant_id,
                    "topic_path": topic_path,
                }
            ),
            status=200,
            mimetype="application/json",
        )

    except Exception as e:
        print(f"Error in assistant_update_webhook: {str(e)}")
        traceback.print_exc()
        return Response(
            response=json.dumps({"error": str(e)}),
            status=500,
            mimetype="application/json",
        )
