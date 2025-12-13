import asyncio
import base64
import json
import os
import re
import traceback
import requests
from functools import wraps
from urllib.parse import quote_plus

from fastapi import Request, Response

from google.cloud import pubsub_v1

from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse
from livekit import api

from azure.identity import ClientSecretCredential
from msgraph import GraphServiceClient
from msgraph.generated.users.item.messages.item.message_item_request_builder import (
    MessageItemRequestBuilder,
)

# Azure AD credentials from environment
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")

STAGING = os.getenv("STAGING")
ORCHESTRA_URL = (
    "https://api.unify.ai/v0"
    if not STAGING
    else "https://service.a.run.app/v0"
)
COMMS_URL = os.getenv("UNITY_COMMS_URL")
ADAPTERS_URL = os.getenv("UNITY_ADAPTERS_URL")


def get_assistant(
    email_address: str = None,
    phone_number: str = None,
    assistant_id: str = None,
) -> dict[str, str]:
    """
    Get the assistant id from the email address or phone number.

    Args:
        email_address: The email address of the assistant.
        phone_number: The phone number of the assistant.

    Returns:
        The assistant id.
    """
    params = dict()
    if email_address:
        params["email"] = email_address
    if phone_number:
        params["phone"] = phone_number
    if assistant_id:
        params["agent_id"] = assistant_id
    email_check = email_address or ""
    phone_check = phone_number or ""

    default_assistant_data = {
        "assistant_id": "default-assistant",
        "user_id": "default-user",
        "voice_provider": "cartesia",
        "voice_id": None,
        "voice_mode": "tts",
        "api_key": "",
        "user_name": "",
        "assistant_first_name": "Default",
        "assistant_surname": "Assistant",
        "assistant_age": "20",
        "assistant_nationality": "United States",
        "assistant_about": "Default Assistant",
        "assistant_email": "unity.agent@unify.ai",
        "user_email": "unity.agent@unify.ai",
        "user_number": "",
        "assistant_number": "",
        "user_whatsapp_number": "",
        "assistant_whatsapp_number": "",
    }
    if "+15550100002" in phone_check or assistant_id == "default-assistant":
        return default_assistant_data
    if (
        "+0123456789" in phone_check
        or "default-test-assistant@unify.ai" in email_check
        or assistant_id == "default-test-assistant"
    ):
        return {
            **default_assistant_data,
            "assistant_id": "default-test-assistant",
            "user_name": "Test User",
            "user_number": "+9876543210",
            "user_email": "test@unify.ai",
            "assistant_first_name": "Test",
            "assistant_surname": "Assistant",
            "assistant_number": "+0123456789",
            "assistant_email": "default-test-assistant@unify.ai",
            "user_whatsapp_number": "+9876543210",
        }

    response = requests.get(
        f"{ORCHESTRA_URL}/admin/assistant",
        params=params,
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
    ).json()

    print(f"get_assistant params: {params}")
    print(f"get_assistant response: {response}")

    if "detail" in response:
        return {**default_assistant_data, "assistant_id": None}
    assistants = response["info"]
    if len(assistants) == 0:
        return {**default_assistant_data, "assistant_id": None}

    return {
        "assistant_id": assistants[0]["agent_id"],
        "user_id": assistants[0]["user_id"],
        "api_key": assistants[0]["api_key"],
        "user_name": f"{assistants[0]['user_first_name']} {assistants[0]['user_last_name']}",
        "assistant_first_name": assistants[0]["first_name"],
        "assistant_surname": assistants[0]["surname"],
        "assistant_age": assistants[0]["age"],
        "assistant_nationality": assistants[0]["nationality"],
        "assistant_about": assistants[0]["about"],
        "assistant_number": assistants[0]["phone"] or "",
        "assistant_whatsapp_number": assistants[0]["assistant_whatsapp_number"] or "",
        "assistant_email": assistants[0]["email"] or "",
        "user_number": assistants[0]["user_phone"] or "",
        "user_whatsapp_number": assistants[0]["user_whatsapp_number"] or "",
        "user_email": assistants[0]["user_email"] or "",
        "voice_provider": assistants[0]["voice_provider"],
        "voice_id": assistants[0]["voice_id"],
        "voice_mode": assistants[0]["voice_mode"],
    }


def get_contacts(context: str, api_key: str) -> tuple[list[dict[str, str]], int]:
    response = requests.get(
        f"{ORCHESTRA_URL}/logs",
        params={"project": "Assistants", "context": context},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return response.json(), response.status_code


def get_default_contacts(assistant_data: dict) -> list[dict[str, str]]:
    return [
        {
            "contact_id": 0,
            "first_name": assistant_data["assistant_first_name"],
            "surname": assistant_data["assistant_surname"],
            "email_address": assistant_data["assistant_email"],
            "phone_number": assistant_data["assistant_number"],
            "whatsapp_number": assistant_data["assistant_whatsapp_number"],
            "bio": "",
            "rolling_summary": "",
            "respond_to": False,
            "response_policy": "",
        },
        {
            "contact_id": 1,
            "first_name": assistant_data["user_name"],
            "surname": "",
            "email_address": assistant_data["user_email"],
            "phone_number": assistant_data["user_number"],
            "whatsapp_number": assistant_data["user_whatsapp_number"],
            "bio": "",
            "rolling_summary": "",
            "respond_to": False,
            "response_policy": "",
        },
    ]


def check_contact_details(
    email_address: str = None,
    phone_number: str = None,
    medium: str = None,
    user_number: str = None,
    user_whatsapp_number: str = None,
    user_email: str = None,
) -> bool:
    """
    Check if the contact details are valid.

    Args:
        email_address: The email address of the contact.
        phone_number: The phone number of the contact.
        medium: The medium of the contact.
        user_number: The phone number of the user.
        user_whatsapp_number: The whatsapp number of the user.
        user_email: The email of the user.
    """
    print(
        f"Checking contact details: {email_address}, {phone_number}, {medium}, "
        f"{user_number}, {user_whatsapp_number}, {user_email}"
    )
    if medium == "email" and user_email == email_address:
        return True
    if medium in ["msg", "phone"] and user_number == phone_number:
        return True
    if medium == "whatsapp" and user_whatsapp_number == phone_number:
        return True
    return False


def check_valid_contact(
    email_address: str = None,
    phone_number: str = None,
    medium: str = None,
    assistant_context: str = None,
    api_key: str = None,
    user_number: str = None,
    user_whatsapp_number: str = None,
    user_email: str = None,
    assistant_data: dict = None,
) -> list[dict[str, str]]:
    """
    Check if the contact is valid.

    Args:
        email_address: The email address of the contact.
        phone_number: The phone number of the contact.
        medium: The medium of the contact.
        assistant_context: The context of the assistant.
        api_key: The API key of the assistant.
        user_number: The phone number of the user.
        user_whatsapp_number: The whatsapp number of the user.
        user_email: The email of the user.
        assistant_data: The data of the assistant.
    """
    print(
        f"Checking valid contact: {email_address}, {phone_number}, {medium}, "
        f"{user_number}, {user_whatsapp_number}, {user_email}, {assistant_context}"
    )

    # check for contact in assistant contacts
    context = f"{assistant_context}/Contacts"
    response_json, status_code = get_contacts(context, api_key)
    default_contacts = get_default_contacts(assistant_data)
    if status_code != 200:
        # if the context isn't created yet (first time user)
        if response_json["detail"] == f"Context '{context}' not found":
            # check for boss user
            if check_contact_details(
                email_address=email_address,
                phone_number=phone_number,
                medium=medium,
                user_number=user_number,
                user_whatsapp_number=user_whatsapp_number,
                user_email=user_email,
            ):
                print(
                    f"Boss user found: {email_address}, {phone_number}, {medium}, "
                    f"{user_number}, {user_whatsapp_number}, {user_email}"
                )
                return default_contacts, True

        # otherwise
        print(f"Failed to get contacts for assistant {assistant_context}")
        print(response_json)
        return default_contacts, False
    contact_logs = response_json["logs"]
    contacts = [c["entries"] for c in contact_logs]
    print(f"Contacts: {contacts}")
    if len(contacts) == 0:
        return default_contacts, False

    # check for boss user
    boss_contact = [contact for contact in contacts if contact["contact_id"] == 1]
    print(f"Boss contact: {boss_contact}")
    if len(boss_contact) > 0:
        boss_contact = boss_contact[0]
        user_number = boss_contact["phone_number"]
        user_whatsapp_number = boss_contact["whatsapp_number"]
        user_email = boss_contact["email_address"]
        if check_contact_details(
            email_address=email_address,
            phone_number=phone_number,
            medium=medium,
            user_number=user_number,
            user_whatsapp_number=user_whatsapp_number,
            user_email=user_email,
        ):
            print(
                f"Boss user found: {email_address}, {phone_number}, {medium}, "
                f"{user_number}, {user_whatsapp_number}, {user_email}"
            )
            return contacts, True
    else:
        print("No boss user found")
        return default_contacts, False

    # check all contacts
    for contact in contacts:
        if check_contact_details(
            email_address=email_address,
            phone_number=phone_number,
            medium=medium,
            user_number=contact["phone_number"],
            user_whatsapp_number=contact["whatsapp_number"],
            user_email=contact["email_address"],
        ):
            print(f"Contact found: {contact}")
            return contacts, True
    return default_contacts, False


def is_job_running(user_id: str, assistant_id: str):
    print(f"Checking if job is running for {user_id} --> {assistant_id}")
    response = requests.get(
        f"{ORCHESTRA_URL}/logs",
        params={
            "project": "AssistantJobs",
            "context": "startup_events",
            "filter_expr": (
                f"user_id == '{user_id}' and "
                f"assistant_id == '{assistant_id}' and "
                f"running == 'true'"
            ),
        },
        headers={"Authorization": f"Bearer {os.getenv('SHARED_UNIFY_KEY')}"},
    )
    print(f"Response: {response.status_code}")
    if response.status_code != 200:
        return False
    logs = response.json()["logs"]
    return bool(logs)


def start_unity_job(assistant: dict, medium: str):
    """Start the service using values from assistant dict."""
    api_key = assistant["api_key"]
    assistant_id = assistant["assistant_id"]

    if api_key == "":
        print(f"No user name for assistant {assistant_id}")
        return

    # start job
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    try:
        response = requests.post(
            f"{COMMS_URL}/infra/job/start",
            headers=headers,
            data={
                "api_key": api_key,
                "medium": medium,
                "assistant_id": assistant_id,
                "user_id": assistant["user_id"],
                "user_name": assistant["user_name"],
                "user_email": assistant["user_email"],
                "assistant_name": f"{assistant['assistant_first_name']} {assistant['assistant_surname']}",
                "assistant_age": assistant["assistant_age"],
                "assistant_nationality": assistant["assistant_nationality"],
                "assistant_about": assistant["assistant_about"],
                "user_number": assistant["user_number"],
                "assistant_number": assistant["assistant_number"],
                "assistant_email": assistant["assistant_email"],
                "user_whatsapp_number": assistant["user_whatsapp_number"],
                "voice_provider": assistant["voice_provider"],
                "voice_id": assistant["voice_id"],
                "voice_mode": assistant["voice_mode"],
            },
            timeout=1,
        )
        if response.status_code != 200:
            print(f"Failed to start job for assistant {assistant_id}")
        else:
            print(f"Job started for assistant {assistant_id}")
    except requests.exceptions.Timeout:
        print(f"Job started for assistant {assistant_id} (timeout)")


def create_job(assistant_id: str):
    """
    Create idle job by calling the dedicated Cloud Function.
    Uses httpx.Client with minimal timeout for fire-and-forget behavior.
    """

    try:
        # Determine the correct URL based on staging/prod
        idle_job_url = ADAPTERS_URL + "/scheduled/idle-job-creator"
        # Make request with 1 second timeout - just enough to send it
        requests.post(idle_job_url, timeout=1)
        print(f"Idle job creation request sent for assistant {assistant_id}")
        return True
    except requests.exceptions.Timeout as e:
        # timeout exception is expected, just return True
        print(f"Idle job creation request sent for assistant {assistant_id} (timeout)")
        return True
    except Exception as e:
        print(
            f"Error sending idle job creation request for assistant {assistant_id}: {e}"
        )
        return False


def build_webhook_context(
    channel: str,
    destination: str,
    sender: str,
    assistant_id: str = None,
    validate_contact: bool = True,
    ensure_job: bool = True,
    force_start: bool = False,
):
    """Build a shared context for webhooks."""
    # normalize identifiers and resolve assistant by channel
    is_email = channel == "email"
    normalized_sender = (
        sender.replace("whatsapp:", "") if channel == "whatsapp" else sender
    )

    # get assistant data
    if assistant_id:
        assistant_data = get_assistant(assistant_id=assistant_id)
    else:
        print(f"Getting assistant data for {destination} with is_email: {is_email}")
        assistant_data = (
            get_assistant(email_address=destination)
            if is_email
            else get_assistant(phone_number=destination)
        )
    api_key = assistant_data["api_key"]
    assistant_id = assistant_data["assistant_id"]
    user_id = assistant_data["user_id"]
    assistant_first_name = assistant_data["assistant_first_name"]
    assistant_surname = assistant_data["assistant_surname"]
    user_number = assistant_data["user_number"]
    user_whatsapp_number = assistant_data["user_whatsapp_number"]
    user_email = assistant_data["user_email"]
    print("assistant_data:", assistant_data)

    # validate contact
    contacts = []
    is_valid_contact = True
    print("validate_contact:", validate_contact)
    if validate_contact and assistant_id not in [4, 5, 6, 7]:
        contacts, is_valid_contact = check_valid_contact(
            email_address=(sender if is_email else ""),
            phone_number=("" if is_email else normalized_sender),
            medium=channel,
            assistant_context=f"{assistant_first_name}{assistant_surname}",
            api_key=api_key,
            user_number=user_number,
            user_whatsapp_number=user_whatsapp_number,
            user_email=user_email,
            assistant_data=assistant_data,
        )
    else:
        contacts, status_code = get_contacts(
            f"{assistant_first_name}{assistant_surname}/Contacts", api_key
        )
        if status_code != 200:
            contacts = get_default_contacts(assistant_data)
        else:
            contacts = [c["entries"] for c in contacts["logs"]]
    print("contacts:", contacts)

    # check contact validity
    is_default_assistant = (
        "default" in assistant_id
        or "Default Assistant" in assistant_data["assistant_about"]
    )
    is_test_assistant = "test" in assistant_id
    is_valid_contact = is_valid_contact or is_default_assistant

    # ensure job is running (skip for tests/default)
    job_started = False
    is_running = is_job_running(user_id, assistant_id)
    skip_auto_start = is_test_assistant or is_default_assistant or is_running
    should_start_job = (
        ensure_job and is_valid_contact and (force_start or not skip_auto_start)
    )
    if should_start_job:
        start_unity_job(assistant_data, channel)
        create_job(assistant_id)
        job_started = True
        is_running = True

    print("is_valid_contact:", is_valid_contact)
    return {
        "assistant": assistant_data,
        "contacts": contacts,
        "is_valid_contact": is_valid_contact,
        "is_job_running": is_running,
        "job_started": job_started,
    }


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
    recording_status_callback = (
        f"{COMMS_URL}/phone/recording?" f"conference_name={quote_plus(conference_name)}"
    )
    print("Recording status callback: ", recording_status_callback)
    if with_status:
        dial_user.conference(
            conference_name,
            startConferenceOnEnter=True,
            endConferenceOnExit=True,
            muted=False,
            wait_url="https://auburn-eagle-6359.twil.io/assets/ring-tone-68676.mp3",
            record="record-from-start",
            recording_status_callback=recording_status_callback,
            recording_status_callback_event="completed",
            status_callback=f"{COMMS_URL}/phone/conference-status",
            status_callback_event="end",
        )
        return resp_user
    dial_user.conference(
        conference_name,
        startConferenceOnEnter=True,
        endConferenceOnExit=True,
        muted=False,
        wait_url="https://auburn-eagle-6359.twil.io/assets/ring-tone-68676.mp3",
        record="record-from-start",
        recording_status_callback=recording_status_callback,
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


# =============================================================================
# Outlook Helpers
# =============================================================================


def get_graph_client():
    """
    Create a Microsoft Graph client using client credentials flow.
    """
    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        raise Exception("Azure AD credentials not configured.")

    credential = ClientSecretCredential(
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
    )
    return GraphServiceClient(
        credentials=credential,
        scopes=["https://graph.microsoft.com/.default"],
    )


async def get_outlook_thread_id(user_email: str, email_id: str, graph_client):
    """
    Fetch Outlook message details and mark as read.
    Similar to get_thread_id for Gmail - extracts conversation data from a notification.

    Returns:
        tuple: (conversation_id, email_id, last_message) or (None, None, None) if not found
    """
    try:
        # Fetch the message with body in text format (not HTML)
        # Must explicitly select uniqueBody as it's not returned by default
        request_config = (
            MessageItemRequestBuilder.MessageItemRequestBuilderGetRequestConfiguration()
        )
        request_config.headers.add("Prefer", 'outlook.body-content-type="text"')
        request_config.query_parameters = (
            MessageItemRequestBuilder.MessageItemRequestBuilderGetQueryParameters(
                select=[
                    "id",
                    "conversationId",
                    "subject",
                    "body",
                    "uniqueBody",
                    "from",
                    "toRecipients",
                    "ccRecipients",
                    "bccRecipients",
                    "receivedDateTime",
                    "hasAttachments",
                ]
            )
        )

        message = (
            await graph_client.users.by_user_id(user_email)
            .messages.by_message_id(email_id)
            .get(request_configuration=request_config)
        )

        if not message:
            print(f"Message {email_id} not found")
            return None, None, None

        # Note: Not marking as read - subscription only triggers on "created" events,
        # so we don't need to track read status for duplicate prevention

        # Extract message details (similar to Gmail's last_message format)
        # Use unique_body to get only the new content, not the quoted thread history
        last_message = {
            "sender": message.from_.email_address.address if message.from_ else "",
            "to": [r.email_address.address for r in (message.to_recipients or [])],
            "cc": [r.email_address.address for r in (message.cc_recipients or [])],
            "bcc": [r.email_address.address for r in (message.bcc_recipients or [])],
            "subject": message.subject or "",
            "content": message.unique_body.content if message.unique_body else "",
            "received_at": (
                message.received_date_time.isoformat()
                if message.received_date_time
                else None
            ),
            "has_attachments": message.has_attachments,
            "attachments": [],  # TODO: fetch attachment details if needed
        }

        conversation_id = message.conversation_id
        print(
            f"conversation_id: {conversation_id}, email_id: {email_id}, last_message: {last_message}"
        )

        return conversation_id, email_id, last_message

    except Exception as e:
        print(f"Error fetching Outlook message: {e}")
        traceback.print_exc()
        return None, None, None


# =============================================================================
# Gmail Helpers
# =============================================================================


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


def _collect_attachments(payload):
    attachments = []
    if not payload:
        return attachments
    body = payload.get("body", {})
    filename = payload.get("filename")
    attachment_id = body.get("attachmentId")
    mime_type = payload.get("mimeType")
    size = body.get("size")
    if attachment_id:
        attachments.append(
            {
                "id": attachment_id,
                "filename": filename or "",
                "mimeType": mime_type,
                "size": size,
            }
        )
    for part in payload.get("parts", []):
        attachments.extend(_collect_attachments(part))
    return attachments


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
                "subject": _header(headers, "Subject").replace("Re: ", ""),
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
            )
            .execute()
        )
        print(f"pre-histories: {histories}")

        # Safeguard for thread replies
        if "history" not in histories or not histories["history"]:
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

        # Process each history entry
        print(f"histories: {histories}")
        for history in histories["history"]:
            print(f"history: {history}")
            messages = history.get("messages", [])
            print(f"messages: {messages}")
            if len(messages) == 0:
                continue

            # Get the message details (Gmail message resource id)
            msg_id = messages[-1]["id"]
            message = (
                gmail_service.users()
                .messages()
                .get(userId=user_id, id=msg_id)
                .execute()
            )
            print(f"message: {message} {msg_id}")
            message_headers = message["payload"].get("headers", [])
            print(f"message_headers: {message_headers}")
            message_id_header = [
                header
                for header in message_headers
                if header.get("name") == "Message-ID"
            ][0]
            email_id = message_id_header.get("value")
            print(f"email_id: {email_id}")

            # Extract attachments from the Gmail message payload
            attachments = _collect_attachments(message.get("payload", {}))
            print(f"attachments: {attachments}")

            labels = message.get("labelIds", [])
            print(f"labels: {labels}")
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
            print(f"thread: {thread} {thread_id}")

            # Convert to conversation format
            conversation = _gmail_thread_to_conversation(thread)
            print(f"conversation: {conversation}")
            last_message = conversation[-1]
            print(f"last_message: {last_message}")

            # Attach filenames with IDs to the last_message for publishing
            last_message["attachments"] = [
                {"id": att["id"], "filename": att.get("filename", "")}
                for att in attachments
            ]

            # Return the conversation plus Gmail message id
            return thread_id, email_id, last_message, msg_id

        return None, None, None, None

    except Exception as e:
        print(f"Error processing history for user {user_id}: {str(e)}")
        traceback.print_exc()
        return None, None, None, None


def publish_gmail_thread_id(
    assistant_id,
    user_id,
    thread_id,
    email_id,
    last_message,
    contacts,
    gmail_message_id=None,
):
    """Publish the thread_id and user_id to a different pub/sub topic."""
    try:
        publisher = pubsub_v1.PublisherClient()
        topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
        topic_path = publisher.topic_path(os.getenv("PROJECT_ID"), topic_name)

        message_dict = {
            "thread": "email",
            "event": {
                "contacts": contacts,
                "thread_id": thread_id,
                "email_id": email_id,
                "gmail_message_id": gmail_message_id,
                "attachments": last_message.get("attachments", []),
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
        publish_future = publisher.publish(topic_path, data=data)
        if "test" in assistant_id:
            pubsub_message_id = publish_future.result(timeout=10)
            print(f"Message ID: {pubsub_message_id}")
        print(f"Published thread_id {thread_id} for user {user_id} to {topic_path}")
    except Exception as e:
        print(f"Failed to publish thread_id {thread_id} for user {user_id}: {e}")


def publish_outlook_thread_id(
    assistant_id,
    user_id,
    conversation_id,
    email_id,
    last_message,
    contacts,
):
    """Publish the Outlook conversation to pub/sub topic."""
    try:
        publisher = pubsub_v1.PublisherClient()
        topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
        topic_path = publisher.topic_path(os.getenv("PROJECT_ID"), topic_name)

        message_dict = {
            "thread": "email",
            "event": {
                "contacts": contacts,
                "thread_id": conversation_id,
                "email_id": email_id,
                "attachments": last_message.get("attachments", []),
                "from": last_message["sender"],
                "to": last_message["to"],
                "cc": last_message["cc"],
                "bcc": last_message.get("bcc", ""),
                "subject": last_message["subject"],
                "body": last_message["content"],
            },
        }
        data = json.dumps(message_dict).encode("utf-8")

        publish_future = publisher.publish(topic_path, data=data)
        if "test" in assistant_id:
            msg_id = publish_future.result(timeout=10)
            print(f"Message ID: {msg_id}")
        print(
            f"Published conversation_id {conversation_id} for user {user_id} to {topic_path}"
        )
    except Exception as e:
        print(
            f"Failed to publish conversation_id {conversation_id} for user {user_id}: {e}"
        )


def dispatch_agent(agent_name: str):
    response = requests.post(
        f"{COMMS_URL}/phone/dispatch-agent",
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
        json={"agent_name": agent_name},
    )
    if response.status_code != 200:
        print(f"Failed to dispatch agent. Status: {response.status_code}")
        return False
    return True
