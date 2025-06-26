import base64
import json
import os
import re
import requests
from google.cloud import pubsub_v1


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

        # if not histories or "history" not in histories or not histories["history"]:
        #     histories = (
        #         gmail_service.users()
        #         .history()
        #         .list(
        #             userId=user_id,
        #             startHistoryId=history_id,
        #             labelId="UNREAD",
        #         )
        #         .execute()
        #     )

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

            # labels = message.get("labelIds", [])
            # if (labels and "UNREAD" not in labels) or (msg_id == messages[-1]["threadId"]):
            #     print(f"Message {msg_id} is read, skipping")
            #     continue

            # gmail_service.users().messages().modify(
            #     userId=user_id, id=msg_id, body={"removeLabelIds":["UNREAD"]}
            # ).execute()

            # Get the thread for this message
            thread_id = message["threadId"]
            thread = (
                gmail_service.users()
                .threads()
                .get(userId=user_id, id=thread_id, format="full")
                .execute()
            )

            thread_msg = thread.get("messages", [])
            if not thread_msg or (thread_msg and thread_msg[-1]["labelIds"] and "UNREAD" not in thread_msg[-1]["labelIds"]):
                print(f"Message {thread_msg[-1]['id']} is read, skipping")
                continue

            gmail_service.users().messages().modify(
                userId=user_id, id=thread_msg[-1]["id"], body={"removeLabelIds":["UNREAD"]}
            ).execute()

            print("Thread: ", thread_msg)

            # Convert to conversation format
            conversation = _gmail_thread_to_conversation(thread)

            # ToDo: check if the conversation has changed
            # if it has, send the thread_id to a different channel
            # if it hasn't, return None

            # Return the conversation (or process it further as needed)
            return thread_id

    except Exception as e:
        print(f"Error processing history for user {user_id}: {str(e)}")
        return None


def publish_thread_id(assistant_id, thread_id, user_id):
    """Publish the thread_id and user_id to a different pub/sub topic."""
    try:
        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(
            os.getenv("PROJECT_ID"), f"unity-{assistant_id}"
        )

        message_dict = {
            "thread": "email",
            "event": {
                "thread_id": thread_id,
                "email": user_id,
            },
        }
        data = json.dumps(message_dict).encode("utf-8")

        # Publish asynchronously
        future = publisher.publish(topic_path, data=data)
        future.result()  # Wait for publish to complete
        print(f"Published thread_id {thread_id} for user {user_id} to {topic_path}")
    except Exception as e:
        print(f"Failed to publish thread_id {thread_id} for user {user_id}: {e}")


def get_assistant_and_voice_info(
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
        if "+15550100002" in phone_number:
            return "default-assistant", "cartesia", None
        if "+15550100001" in phone_number:
            return "default-assistant-2", "cartesia", None
        if "+15550100005" in phone_number:
            return "default-assistant-3", "cartesia", None
    response = requests.get(
        "https://api.unify.ai/v0/admin/assistant",
        params=params,
        headers={"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"},
    ).json()
    if response and "detail" in response:
        return "default-assistant", "cartesia", None
    assistants = response["info"]
    if len(assistants) == 0:
        return "default-assistant", "cartesia", None
    return (
        assistants[0]["agent_id"],
        "cartesia",#assistants[0]["tts_provider"],
        assistants[0]["voice_id"],
    )


def start_service_if_not_running(assistant_id: str):
    """
    Start the service if it is not running.
    """
    if assistant_id not in ["default-assistant", "default-assistant-2", "default-assistant-3"]:
        service_url = f"https://unity-{assistant_id}-000000000000.us-central1.run.app"
        headers = {"Authorization": f"Bearer {os.getenv('ORCHESTRA_ADMIN_KEY')}"}
        response = requests.get(f"{service_url}/status", headers=headers).json()
        if not response["running"]:
            response = requests.post(f"{service_url}/start", headers=headers)
            if response.status_code != 200:
                print(f"Failed to start service for assistant {assistant_id}")
