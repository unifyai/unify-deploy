import asyncio
import base64
import json
import os
import re
import requests
import httpx
from google.cloud import pubsub_v1


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
            return None

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

    except Exception as e:
        print(f"Error processing history for user {user_id}: {str(e)}")
        return None


def publish_thread_id(assistant_id, thread_id, user_id, last_message):
    """Publish the thread_id and user_id to a different pub/sub topic."""
    try:
        publisher = pubsub_v1.PublisherClient()
        topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
        topic_path = publisher.topic_path(os.getenv("PROJECT_ID"), topic_name)

        message_dict = {
            "thread": "email",
            "event": {
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
        "assistant_name": "Default Assistant",
        "assistant_age": "20",
        "assistant_region": "United States",
        "assistant_about": "Default Assistant",
        "assistant_email": "unity.agent@unify.ai",
        "user_email": "unity.agent@unify.ai",
        "user_number": "",
        "assistant_number": "",
        # "user_phone_number": "",
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
            "assistant_name": "Lily",
            "assistant_age": "25",
            "assistant_number": "+15550100001",
            "assistant_email": "unity.agent@unify.ai",
            # "user_phone_number": "+15550100004",
            "tts_provider": "cartesia",
            "voice_id": None,
        }
    if "+15550100005" in phone_check or "user@example.com" in email_check:
        return {
            **default_assistant_data,
            "api_key": "",
            "assistant_id": "default-assistant-3",
            "user_name": "Ved",
            "user_number": "+15550100004",
            "assistant_number": "+15550100005",
            # "user_phone_number": "+15550100004",
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
        "assistant_name": f"{assistants[0]['first_name']} {assistants[0]['surname']}",
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
            )
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
    user_phone_number: str,
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
        user_phone_number: The phone number of the user.
        user_email: The email of the user.
        tts_provider: The tts provider of the assistant.
        voice_id: The voice id of the assistant.
    """
    # default option when api key isn't set
    if api_key == "":
        print(f"No user name for assistant {assistant_id}")
        return

    # get commit hash
    headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
    response = requests.get(
        f"{COMMS_URL}/infra/image",
        headers=headers,
    )
    if response.status_code != 200:
        print(f"Failed to get commit hash for assistant {assistant_id}")
        return
    commit_hash = response.json()["commit_hash"]
    image = (
        "us-central1-docker.pkg.dev/gcp-project-runtime/unity"
        + ("/unity:" if not STAGING else "/unity-staging:")
        + commit_hash
    )

    # start job
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
            "user_phone_number": user_phone_number,
            "tts_provider": tts_provider,
            "voice_id": voice_id,
        },
    )
    if response.status_code != 200:
        print(f"Failed to start job for assistant {assistant_id}")
    else:
        print(f"Job started for assistant {assistant_id}")

    # create job
    def create_job():
        response = requests.post(
            f"{COMMS_URL}/infra/job/create",
            headers=headers,
            data={"image": image},
        )
        if response.status_code != 200:
            print(f"Failed to create job for assistant {assistant_id}")
            print(f"Error: {response.text}")
        else:
            print(f"Job creation initiated for assistant {assistant_id}")

    # Start job creation asynchronously without waiting
    asyncio.run(asyncio.to_thread(create_job))
