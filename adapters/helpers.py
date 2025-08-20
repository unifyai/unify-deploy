import base64
import json
import os
import re
import requests
import httpx

from google.cloud import pubsub_v1

from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse
from livekit import api


STAGING = os.getenv("STAGING")
ORCHESTRA_URL = (
    "https://api.unify.ai/v0"
    if not STAGING
    else "https://service.a.run.app/v0"
)
COMMS_URL = (
    "https://unity-comms-app-000000000000.us-central1.run.app"
    if not STAGING
    else "https://unity-comms-app-staging-000000000000.us-central1.run.app"
)


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
    email_check = email_id or ""
    phone_check = phone_number or ""

    default_assistant_data = {
        "assistant_id": "default-assistant",
        "user_id": "default-user",
        "tts_provider": "cartesia",
        "voice_id": None,
        "api_key": "",
        "user_name": "",
        "assistant_first_name": "Default",
        "assistant_surname": "Assistant",
        "assistant_age": "20",
        "assistant_region": "United States",
        "assistant_about": "Default Assistant",
        "assistant_email": "unity.agent@unify.ai",
        "user_email": "unity.agent@unify.ai",
        "user_number": "",
        "assistant_number": "",
        "user_whatsapp_number": "",
    }
    if "+15550100002" in phone_check:
        return default_assistant_data
    if "+15550100001" in phone_check or "julia@unify.ai" in email_check:
        return {
            **default_assistant_data,
            "api_key": "",
            "assistant_id": "default-assistant-2",
            "user_name": "Julia",
            "user_number": "+18125625087",
            "user_email": "julia@unify.ai",
            "assistant_first_name": "Lily",
            "assistant_age": "25",
            "assistant_number": "+15550100001",
            "assistant_email": "unity.agent@unify.ai",
            "user_whatsapp_number": "+15550100004",
            "tts_provider": "cartesia",
            "voice_id": None,
        }
    if "+15550100005" in phone_check or "default-assistant-3@unify.ai" in email_check:
        return {
            **default_assistant_data,
            "api_key": "",
            "assistant_id": "default-assistant-3",
            "user_name": "Ved",
            "user_number": "+15550100004",
            "user_email": "user@example.com",
            "assistant_first_name": "Liz",
            "assistant_age": "25",
            "assistant_region": "United States",
            "assistant_about": "Default Assistant",
            "assistant_number": "+15550100005",
            "assistant_email": "default-assistant-3@unify.ai",
            "user_whatsapp_number": "+15550100004",
            "tts_provider": "elevenlabs",
            "voice_id": "ThT5KcBeYPX3keUQqHPh",
        }
    if "+15550100007" in phone_check:
        return {**default_assistant_data, "assistant_id": "default-assistant-4"}
    if "+15550100008" in phone_check:
        return {**default_assistant_data, "assistant_id": "default-assistant-5"}

    response = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
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
        "user_id": assistants[0]["user_id"],
        "api_key": assistants[0]["api_key"],
        "user_name": f"{assistants[0]['user_first_name']} {assistants[0]['user_last_name']}",
        "assistant_first_name": assistants[0]["first_name"],
        "assistant_surname": assistants[0]["surname"],
        "assistant_age": assistants[0]["age"],
        "assistant_region": assistants[0]["region"],
        "assistant_about": assistants[0]["about"],
        "assistant_number": assistants[0]["phone"],
        "assistant_whatsapp_number": assistants[0]["assistant_whatsapp_number"],
        "assistant_email": assistants[0]["email"],
        "user_number": assistants[0]["user_phone"],
        "user_whatsapp_number": assistants[0]["user_whatsapp_number"],
        "user_email": assistants[0]["user_email"],
        "tts_provider": assistants[0]["tts_provider"],
        "voice_id": assistants[0]["voice_id"],
    }


def check_contact_details(
    email_id: str = None,
    phone_number: str = None,
    medium: str = None,
    user_number: str = None,
    user_whatsapp_number: str = None,
    user_email: str = None,
) -> bool:
    """
    Check if the contact details are valid.

    Args:
        email_id: The email id of the contact.
        phone_number: The phone number of the contact.
        medium: The medium of the contact.
        user_number: The phone number of the user.
        user_whatsapp_number: The whatsapp number of the user.
        user_email: The email of the user.
    """
    if medium == "email" and user_email == email_id:
        return True
    if medium in ["msg", "phone"] and user_number == phone_number:
        return True
    if medium == "whatsapp" and user_whatsapp_number == phone_number:
        return True
    return False


def check_valid_contact(
    email_id: str = None,
    phone_number: str = None,
    medium: str = None,
    assistant_context: str = None,
    api_key: str = None,
    user_number: str = None,
    user_whatsapp_number: str = None,
    user_email: str = None,
) -> bool:
    """
    Check if the contact is valid.

    Args:
        email_id: The email id of the contact.
        phone_number: The phone number of the contact.
        medium: The medium of the contact.
        assistant_context: The context of the assistant.
        api_key: The API key of the assistant.
        user_number: The phone number of the user.
        user_whatsapp_number: The whatsapp number of the user.
        user_email: The email of the user.
    """
    print(
        f"Checking valid contact: {email_id}, {phone_number}, "
        f"{medium}, {user_number}, {user_whatsapp_number}, {user_email}"
    )

    # check for contact in assistant contacts
    context = f"{assistant_context}/Contacts"
    response = requests.get(
        f"{ORCHESTRA_URL}/logs",
        params={"project": "Assistants", "context": context},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if response.status_code != 200:
        # if the context isn't created yet (first time user)
        if response.json()["detail"] == f"Context '{context}' not found":
            # check for boss user
            if check_contact_details(
                email_id=email_id,
                phone_number=phone_number,
                medium=medium,
                user_number=user_number,
                user_whatsapp_number=user_whatsapp_number,
                user_email=user_email,
            ):
                print(
                    f"Boss user found: {email_id}, {phone_number}, {medium}, "
                    f"{user_number}, {user_whatsapp_number}, {user_email}"
                )
                return {
                    "contact_id": 1,
                    "first_name": "",
                    "surname": "",
                    "email_adress": user_email,
                    "phone_number": phone_number,
                    "whatsapp_number": user_whatsapp_number,
                    "bio": "",
                    "rolling_summary": "",
                    "respond_to": "",
                    "response_policy": "",
                }

        # otherwise
        print(f"Failed to get contacts for assistant {assistant_context}")
        print(response.text)
        return None
    contacts = response.json()["logs"]
    print(f"Contacts: {contacts}")
    if len(contacts) == 0:
        return None

    # check for boss user
    boss_contact = [
        contact for contact in contacts if contact["entries"]["contact_id"] == 1
    ]
    if len(boss_contact) > 0:
        boss_contact = boss_contact[0]
        user_number = boss_contact["entries"]["phone_number"]
        user_whatsapp_number = boss_contact["entries"]["whatsapp_number"]
        user_email = boss_contact["entries"]["email_address"]
        if check_contact_details(
            email_id=email_id,
            phone_number=phone_number,
            medium=medium,
            user_number=user_number,
            user_whatsapp_number=user_whatsapp_number,
            user_email=user_email,
        ):
            print(
                f"Boss user found: {email_id}, {phone_number}, {medium}, "
                f"{user_number}, {user_whatsapp_number}, {user_email}"
            )
            return boss_contact["entries"]
    else:
        print("No boss user found")
        return None

    # check all contacts
    for contact in contacts:
        if check_contact_details(
            email_id=email_id,
            phone_number=phone_number,
            medium=medium,
            user_number=contact["entries"]["phone_number"],
            user_whatsapp_number=contact["entries"]["whatsapp_number"],
            user_email=contact["entries"]["email_address"],
        ):
            print(f"Contact found: {contact}")
            return contact["entries"]
    return None


def is_job_running(user_id: str, assistant_id: str):
    response = requests.get(
        f"{ORCHESTRA_URL}/logs",
        params={
            "project": "Debug",
            "context": "startup_events",
            "filter_expr": (
                f"user_id == '{user_id}' and "
                f"assistant_id == '{assistant_id}' and "
                f"running == 'true'"
            ),
        },
        headers={"Authorization": f"Bearer {os.getenv('SHARED_UNIFY_KEY')}"},
    )
    if response.status_code != 200:
        return False
    logs = response.json()["logs"]
    return bool(logs)


def start_unity_job(
    api_key: str,
    medium: str,
    assistant_id: str,
    user_id: str,
    user_name: str,
    assistant_name: str,
    assistant_age: str,
    assistant_region: str,
    assistant_about: str,
    user_number: str,
    assistant_number: str,
    assistant_email: str,
    user_whatsapp_number: str,
    user_email: str,
    tts_provider: str,
    voice_id: str,
):
    """
    Start the service if it is not running.

    Args:
        api_key: The API key for the assistant.
        medium: The type of medium.
        assistant_id: The ID of the assistant.
        user_id: The ID of the user.
        user_name: The name of the user.
        assistant_name: The name of the assistant.
        assistant_age: The age of the assistant.
        assistant_region: The region of the assistant.
        assistant_about: The about of the assistant.
        user_number: The phone number of the user.
        assistant_number: The phone number of the assistant.
        assistant_email: The email of the assistant.
        user_whatsapp_number: The whatsapp number of the user.
        user_email: The email of the user.
        tts_provider: The tts provider of the assistant.
        voice_id: The voice id of the assistant.
    """
    # default option when api key isn't set
    if api_key == "":
        print(f"No user name for assistant {assistant_id}")
        return

    # start job
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = requests.post(
        f"{COMMS_URL}/infra/job/start",
        headers=headers,
        data={
            "api_key": api_key,
            "medium": medium,
            "assistant_id": assistant_id,
            "user_id": user_id,
            "user_name": user_name,
            "user_email": user_email,
            "assistant_name": assistant_name,
            "assistant_age": assistant_age,
            "assistant_region": assistant_region,
            "assistant_about": assistant_about,
            "user_number": user_number,
            "assistant_number": assistant_number,
            "assistant_email": assistant_email,
            "user_whatsapp_number": user_whatsapp_number,
            "tts_provider": tts_provider,
            "voice_id": voice_id,
        },
    )
    if response.status_code != 200:
        print(f"Failed to start job for assistant {assistant_id}")
    else:
        print(f"Job started for assistant {assistant_id}")


async def create_job(assistant_id: str):
    """
    Create idle job by calling the dedicated Cloud Function.
    Uses httpx.AsyncClient for truly non-blocking request.
    """

    try:
        # Determine the correct URL based on staging/prod
        idle_job_url = (
            "https://us-central1-gcp-project-runtime.cloudfunctions.net/idle-job-creator"
            if not STAGING
            else "https://us-central1-gcp-project-runtime.cloudfunctions.net/idle-job-creator-staging"
        )

        # Use async client for fire-and-forget request
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            # Don't await - just start the request and return immediately
            client.post(idle_job_url, data={"assistant_id": assistant_id})
            print(f"Idle job creation request initiated for assistant {assistant_id}")
            return True

    except Exception as e:
        print(
            f"Error sending idle job creation request for assistant {assistant_id}: {e}"
        )
        return False


# phone helpers
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
            wait_url="https://auburn-eagle-6359.twil.io/assets/ring-tone-68676.mp3",
            record="record-from-start",
            recording_status_callback=f"{COMMS_URL}/phone/recording",
            recording_status_callback_event="completed",
            status_callback=f"{COMMS_URL}/phone/call-status",
            status_callback_event=["completed"],
        )
        return resp_user
    dial_user.conference(
        conference_name,
        startConferenceOnEnter=True,
        endConferenceOnExit=True,
        muted=False,
        wait_url="https://auburn-eagle-6359.twil.io/assets/ring-tone-68676.mp3",
        record="record-from-start",
        recording_status_callback=f"{COMMS_URL}/phone/recording",
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


# email helpers
def _strip_quoted_text(text: str) -> str:
    """Remove quoted text and signatures from email content."""
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(">"):
            continue
        if re.match(r"On .+wrote:", stripped) or stripped.startswith(
            "-----Original Message-----"
        ):
            break
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _header(headers, name: str) -> str:
    """Extract a specific header from email headers."""
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _payload_text(payload) -> str:
    """Extract text content from email payload."""
    mime_type = payload.get("mimeType", "")
    if mime_type.startswith("text/") and payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        decoded = base64.urlsafe_b64decode(data.encode("utf-8"))
        latest = _strip_quoted_text(decoded.decode("utf-8", errors="replace"))
        return latest

    for part in payload.get("parts", []):
        txt = _payload_text(part)
        if txt:
            return txt
    return ""


def _gmail_thread_to_conversation(thread):
    """Convert a Gmail thread to a structured conversation."""
    convo = []
    for msg in thread.get("messages", []):
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        convo.append(
            {
                "sender": _header(headers, "From"),
                "to": (
                    [_addr.strip() for _addr in _header(headers, "To").split(",")]
                    if _header(headers, "To")
                    else []
                ),
                "cc": (
                    [_addr.strip() for _addr in _header(headers, "Cc").split(",")]
                    if _header(headers, "Cc")
                    else []
                ),
                "bcc": (
                    [_addr.strip() for _addr in _header(headers, "Bcc").split(",")]
                    if _header(headers, "Bcc")
                    else []
                ),
                "subject": _header(headers, "Subject"),
                "content": _payload_text(payload),
            }
        )
    return convo


def get_thread_id(user_id, history_id, gmail_service):
    """Process Gmail history and thread to extract conversation data."""
    try:
        # Get history events for label changes
        histories = (
            gmail_service.users()
            .history()
            .list(
                userId=user_id,
                startHistoryId=history_id,
                # historyTypes=["messageAdded", "labelAdded"],
            )
            .execute()
        )

        # Safeguard for thread replies
        if not histories or "history" not in histories or not histories["history"]:
            histories["history"] = [
                (
                    gmail_service.users()
                    .messages()
                    .list(
                        userId=user_id,
                        q="is:unread newer_than:1d",
                    )
                    .execute()
                )
            ]

        if not histories or "history" not in histories or not histories["history"]:
            print(f"No history found for user {user_id} with history id {history_id}")
            return None, None

        # Process each history entry
        print(f"History: {histories}")
        for history in histories["history"]:
            messages = history.get("messages", [])
            if len(messages) == 0:
                continue

            # Get the message details
            msg_id = messages[-1]["id"]
            message = (
                gmail_service.users()
                .messages()
                .get(userId=user_id, id=msg_id)
                .execute()
            )

            labels = message.get("labelIds", [])
            if labels and "UNREAD" not in labels:
                print(f"Message {msg_id} is read, skipping")
                continue

            gmail_service.users().messages().modify(
                userId=user_id, id=msg_id, body={"removeLabelIds": ["UNREAD"]}
            ).execute()

            # Get the thread for this message
            thread_id = message["threadId"]
            thread = (
                gmail_service.users()
                .threads()
                .get(userId=user_id, id=thread_id, format="full")
                .execute()
            )

            # Convert to conversation format
            conversation = _gmail_thread_to_conversation(thread)
            last_message = conversation[-1]

            # Return the conversation (or process it further as needed)
            return thread_id, last_message

        return None, None

    except Exception as e:
        print(f"Error processing history for user {user_id}: {str(e)}")
        return None, None


def publish_thread_id(assistant_id, thread_id, user_id, last_message, contact_id):
    """Publish the thread_id and user_id to a different pub/sub topic."""
    try:
        publisher = pubsub_v1.PublisherClient()
        topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
        topic_path = publisher.topic_path(os.getenv("PROJECT_ID"), topic_name)

        message_dict = {
            "thread": "email",
            "event": {
                "contact_id": contact_id,
                "thread_id": thread_id,
                "from": last_message["sender"],
                "to": last_message["to"],
                "cc": last_message["cc"],
                "bcc": last_message["bcc"],
                "subject": last_message["subject"],
                "body": last_message["content"],
            },
        }
        data = json.dumps(message_dict).encode("utf-8")

        # Publish asynchronously
        future = publisher.publish(topic_path, data=data)
        future.result()  # Wait for publish to complete
        print(f"Published thread_id {thread_id} for user {user_id} to {topic_path}")
    except Exception as e:
        print(f"Failed to publish thread_id {thread_id} for user {user_id}: {e}")
