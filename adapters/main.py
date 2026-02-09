import json
import base64
import traceback
import time
import uuid
from dotenv import load_dotenv
import os
import requests
import httpx
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Optional
from fastapi import FastAPI, File, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import traceback

from google.cloud import pubsub_v1, storage
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

from .helpers import (
    add_user_to_conference,
    build_webhook_context,
    check_valid_contact,
    create_conference_response,
    create_job,
    dispatch_livekit_agent,
    get_assistant,
    get_graph_client_from_token,
    get_outlook_thread_id,
    get_thread_id,
    is_job_running,
    parse_teams_resource_id,
    publish_gmail_thread_id,
    publish_outlook_thread_id,
    exchange_microsoft_code_for_tokens,
    get_microsoft_user_info,
    start_unity_job,
    store_microsoft_tokens,
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
            "console.unify.ai to view your assistant details.",
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
    topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)
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
            print(f"Dispatching LiveKit agent {room_name}")
            dispatch_livekit_agent(room_name)

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

    # Handle call answered (in-progress) or not answered (no-answer, busy, canceled, failed)
    if call_status in ("in-progress", "no-answer", "busy", "canceled", "failed"):
        # get assistant data
        context = build_webhook_context(
            "phone",
            assistant_number,
            user_number,
            validate_contact=False,
        )
        assistant_id = context["assistant"]["assistant_id"]
        contacts = context["contacts"]

        # Determine thread type based on call status
        if call_status == "in-progress":
            thread = "call_answered"
        else:
            # no-answer, busy, canceled, failed are all "not answered" scenarios
            thread = "call_not_answered"

        # publish to pubsub
        pubsub_client = pubsub_v1.PublisherClient()
        topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
        topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)
        print(f"Publishing {thread} to Pub/Sub at path: {topic_path}")
        try:
            publish_future = pubsub_client.publish(
                topic_path,
                json.dumps(
                    {
                        "thread": thread,
                        "event": {
                            "contacts": contacts,
                            "assistant_id": assistant_id,
                            "user_number": user_number,
                            "assistant_number": assistant_number,
                            "call_status": call_status,
                            "timestamp": int(time.time() * 1000),
                        },
                    },
                ).encode("utf-8"),
            )
            if "test" in assistant_id:
                status_id = publish_future.result(timeout=10)
                print(f"Message ID: {status_id}")
            print(f"{thread} published to Pub/Sub successfully")
        except Exception as e:
            print(f"Error publishing to Pub/Sub: {str(e)}")

    return Response(status_code=200)


@app.post("/twilio/sms")
async def twilio_sms_webhook(request: Request):
    """SMS webhook endpoint - handles incoming Twilio SMS messages."""
    print("twilio_sms_webhook function started")
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
            "console.unify.ai to view your assistant details.",
        )
        return Response(content=str(resp_user), media_type="text/xml")

    running = context["is_job_running"]
    print(f"Job running: {running}")

    # set up response
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)
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
                },
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
            "console.unify.ai to view your assistant details.",
        )
        return Response(content=str(resp_user), media_type="text/xml")

    running = context["is_job_running"]
    print(f"Job running: {running}")

    # set up response
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = pubsub_v1.PublisherClient()
    topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)
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
                },
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
# Teams SIP Webhooks
# =============================================================================


@app.post("/teams/call")
async def teams_call_webhook(request: Request):
    """
    Webhook called by Kamailio SBC when a Teams call arrives.
    Publishes to Pub/Sub to trigger agent dispatch, then returns
    so Kamailio can forward the call to LiveKit.
    """
    print("teams_call_webhook function started")

    # Accept JSON from Kamailio
    try:
        data = await request.json()
    except Exception:
        # Fallback to form data if JSON parsing fails
        form_data = await request.form()
        data = dict(form_data)

    # Extract call details from Kamailio
    # Kamailio sends: from_uri, to_uri, call_id
    from_uri = data.get("from_uri", "")
    to_uri = data.get("to_uri", "")
    call_id = data.get("call_id", "")
    source_ip = data.get("source_ip", "")

    print(f"Teams call: from={from_uri} to={to_uri} call_id={call_id}")

    # Extract phone number from to_uri
    # Format: sip:+19999999999@sbc.unify.ai:5061;user=phone;transport=tls
    # We need: +19999999999
    teams_number = ""
    if "sip:" in to_uri:
        # Extract the user part (before @)
        user_part = to_uri.split("sip:")[1].split("@")[0]
        # Clean up any parameters
        teams_number = user_part.split(";")[0]
        if not teams_number.startswith("+"):
            teams_number = "+" + teams_number

    print(f"Extracted Teams number: {teams_number}")

    if not teams_number:
        print("ERROR: Could not extract phone number from to_uri")
        return Response(
            content=json.dumps({"error": "Invalid to_uri"}),
            status_code=400,
            media_type="application/json",
        )

    # Look up assistant by the Teams virtual number
    # This uses the same flow as Twilio - the number maps to an assistant
    try:
        context = build_webhook_context(
            "meet",
            teams_number,
            from_uri,
            validate_contact=False,
        )
        assistant_id = context["assistant"]["assistant_id"]
        contacts = context["contacts"]
    except Exception as e:
        print(f"ERROR: Could not find assistant for {teams_number}: {e}")
        # Return success anyway - let the call proceed, it just won't have an agent
        return Response(
            content=json.dumps({"success": True, "warning": "No assistant found"}),
            status_code=200,
            media_type="application/json",
        )

    # Generate room name (consistent with Twilio pattern)
    room_name = f"unity_{teams_number}"
    sip_uri = f"sip:{teams_number}@{os.getenv('LIVEKIT_SIP_URI')}"

    # print(f"Teams call for assistant {assistant_id}, room: {room_name}")

    # Publish to Pub/Sub (same format as Twilio webhook)
    pubsub_client = pubsub_v1.PublisherClient()
    # topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
    topic_name = "test"
    topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)
    print(f"Publishing Teams call to Pub/Sub at path: {topic_path}")

    try:
        pubsub_message = {
            "thread": "call",
            "event": {
                "contacts": contacts,
                "conference_name": f"Teams_{teams_number[1:]}_{call_id[:8]}",
                "caller_number": from_uri,  # Will be anonymous for Teams AA
                "sip_uri": sip_uri,
                "livekit_room": room_name,
                "assistant_id": assistant_id,
                "action": "start_worker",
                "timestamp": int(time.time() * 1000),
                "call_metadata": {
                    "teams_number": teams_number,
                    "call_type": "inbound_teams",
                    "source": "teams_direct_routing",
                    "call_id": call_id,
                    "source_ip": source_ip,
                },
            },
        }
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(pubsub_message).encode("utf-8"),
        )
        # Don't wait for result - fire and forget for speed
        print("Teams call published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing Teams call to Pub/Sub: {str(e)}")
        # Still return success - don't block the call

    # Return success so Kamailio can proceed to forward to LiveKit
    return Response(
        content=json.dumps(
            {
                "success": True,
                "room_name": room_name,
                # "assistant_id": assistant_id,
            },
        ),
        status_code=200,
        media_type="application/json",
    )


# =============================================================================
# Unify Webhooks
# =============================================================================


class UnifyMessagePayload(BaseModel):
    assistant_id: str
    contact_id: int  # Required - no default to prevent silent privilege escalation
    body: Optional[str] = ""


# =============================================================================
# Unify Attachment Upload
# =============================================================================

# Bucket for storing unify message attachments (single bucket for all environments)
UNIFY_ATTACHMENTS_BUCKET = "unify-message-attachments"

# File type validation - allowed extensions
ALLOWED_EXTENSIONS = {
    # Images
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".bmp",
    ".ico",
    # Documents
    ".pdf",
    ".doc",
    ".docx",
    ".txt",
    ".rtf",
    ".odt",
    # Spreadsheets
    ".xls",
    ".xlsx",
    ".csv",
    ".ods",
    # Presentations
    ".ppt",
    ".pptx",
    ".odp",
    # Archives (for document bundles)
    ".zip",
    # Data
    ".json",
    ".xml",
    ".yaml",
    ".yml",
}

# Blocked extensions (executables, scripts)
BLOCKED_EXTENSIONS = {
    ".exe",
    ".bat",
    ".cmd",
    ".sh",
    ".ps1",
    ".dll",
    ".so",
    ".dylib",
    ".app",
    ".msi",
    ".com",
    ".scr",
    ".vbs",
    ".js",
    ".jse",
    ".wsf",
    ".wsh",
    ".psc1",
    ".reg",
    ".inf",
    ".lnk",
    ".pif",
}

# Allowed MIME types
ALLOWED_MIME_TYPES = {
    # Images
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "image/bmp",
    "image/x-icon",
    # Documents
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "application/rtf",
    "application/vnd.oasis.opendocument.text",
    # Spreadsheets
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
    "application/vnd.oasis.opendocument.spreadsheet",
    # Presentations
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.presentation",
    # Archives
    "application/zip",
    # Data
    "application/json",
    "application/xml",
    "text/xml",
    "application/x-yaml",
    "text/yaml",
    # Generic (for unknown but allowed extensions)
    "application/octet-stream",
}

# Maximum attachments per message
MAX_ATTACHMENTS_PER_MESSAGE = 10


def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename to prevent path traversal attacks.
    Handles both Unix (/) and Windows (\\) path separators.
    """
    # Replace backslashes with forward slashes for consistent handling
    normalized = filename.replace("\\", "/")
    # Extract just the filename (basename)
    basename = os.path.basename(normalized)
    # Remove any remaining path traversal attempts
    basename = basename.replace("..", "")
    # If empty after sanitization, use default
    return basename if basename else "attachment"


def get_file_extension(filename: str) -> str:
    """Get lowercase file extension including the dot."""
    _, ext = os.path.splitext(filename)
    return ext.lower()


def validate_file_type(filename: str, content_type: str) -> tuple[bool, str]:
    """
    Validate file type against blocklist and allowlist.

    Returns (is_valid, error_message).
    """
    ext = get_file_extension(filename)

    # Check blocklist first (security)
    if ext in BLOCKED_EXTENSIONS:
        return False, f"File type '{ext}' is blocked for security reasons"

    # Check extension allowlist
    if ext and ext not in ALLOWED_EXTENSIONS:
        return (
            False,
            f"File type '{ext}' is not allowed. Allowed types: images, documents, spreadsheets, presentations, zip, json, xml, yaml",
        )

    # Check MIME type (but be lenient - some clients send wrong MIME types)
    # Only block if MIME type is clearly executable
    blocked_mimes = {
        "application/x-msdownload",
        "application/x-msdos-program",
        "application/x-sh",
        "application/x-shellscript",
    }
    if content_type in blocked_mimes:
        return False, f"MIME type '{content_type}' is blocked for security reasons"

    return True, ""


@app.post("/unify/attachment")
async def unify_attachment_upload(
    request: Request,
    file: UploadFile = File(...),
    assistant_id: str = None,
):
    """
    Upload a file attachment for use in Unify messages.

    The file is stored in GCS and both permanent (gs://) and signed URLs are returned.
    The returned attachment object can be included in /unify/message requests.

    Args:
        file: The file to upload (multipart/form-data)
        assistant_id: Optional assistant ID for organizing storage

    Returns:
        JSON with attachment details including:
        - id: Unique attachment ID
        - filename: Sanitized filename
        - gs_url: Permanent GCS URL (gs://bucket/path)
        - url: Signed download URL (temporary, for backwards compatibility)
        - content_type: MIME type
        - size_bytes: File size in bytes
    """
    print("unify_attachment_upload function started")

    # Optional auth via admin key
    shared_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    auth_header = request.headers.get("Authorization", "")
    if shared_key and auth_header != f"Bearer {shared_key}":
        print("Unauthorized unify_attachment request")
        return Response(status_code=401)

    # Get assistant_id from form data if not provided as query param
    if not assistant_id:
        form_data = await request.form()
        assistant_id = form_data.get("assistant_id", "unknown")

    try:
        # Read file content
        file_content = await file.read()
        filename = file.filename or "attachment"
        content_type = file.content_type or "application/octet-stream"
        file_size = len(file_content)

        # Sanitize filename (handles both Unix and Windows path separators)
        safe_filename = sanitize_filename(filename)

        # Validate file type
        is_valid, error_msg = validate_file_type(safe_filename, content_type)
        if not is_valid:
            print(f"File type validation failed: {error_msg}")
            return Response(
                content=json.dumps({"error": error_msg}),
                status_code=400,
                media_type="application/json",
            )

        # Validate file size (max 25MB to match Gmail limit)
        max_size_bytes = 25 * 1024 * 1024
        if file_size > max_size_bytes:
            file_size_mb = file_size / (1024 * 1024)
            return Response(
                content=json.dumps(
                    {
                        "error": f"File too large: {file_size_mb:.1f}MB exceeds 25MB limit",
                    },
                ),
                status_code=400,
                media_type="application/json",
            )

        # Generate unique ID for the attachment
        attachment_id = str(uuid.uuid4())

        # Look up user_id from assistant_id via Orchestra API
        # This ensures attachments are stored under the user's path for cleanup
        try:
            assistant_data = get_assistant(assistant_id=assistant_id)
            user_id = assistant_data.get("user_id")
            if user_id and user_id != "default-user":
                path_prefix = str(user_id)
            else:
                # Fallback to assistant_id if user lookup fails
                print(
                    f"Could not get user_id for assistant {assistant_id}, using assistant_id"
                )
                path_prefix = assistant_id
        except Exception as e:
            print(f"Error looking up user for assistant {assistant_id}: {e}")
            path_prefix = assistant_id

        # Build GCS path: {user_id}/{uuid}_{filename}
        blob_path = f"{path_prefix}/{attachment_id}_{safe_filename}"

        # Get GCP credentials and upload
        creds_json = json.loads(os.getenv("GCP_SA_KEY", "{}"))
        if not creds_json:
            print("GCP_SA_KEY not configured")
            return Response(
                content=json.dumps({"error": "Storage not configured"}),
                status_code=500,
                media_type="application/json",
            )

        creds = Credentials.from_service_account_info(creds_json)
        storage_client = storage.Client(credentials=creds)
        bucket = storage_client.bucket(UNIFY_ATTACHMENTS_BUCKET)
        blob = bucket.blob(blob_path)

        # Upload the file
        blob.upload_from_string(file_content, content_type=content_type)

        # Build permanent gs:// URL
        gs_url = f"gs://{UNIFY_ATTACHMENTS_BUCKET}/{blob_path}"
        print(f"Uploaded attachment to {gs_url}")

        # Generate signed download URL (1 hour expiry for immediate use)
        signed_url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=1),
            method="GET",
        )

        print(f"Generated signed URL for attachment {attachment_id}")

        return Response(
            content=json.dumps(
                {
                    "id": attachment_id,
                    "filename": safe_filename,
                    "gs_url": gs_url,
                    "url": signed_url,  # Backwards compatibility
                    "content_type": content_type,
                    "size_bytes": file_size,
                },
            ),
            status_code=200,
            media_type="application/json",
        )

    except Exception as e:
        print(f"Error uploading attachment: {str(e)}")
        traceback.print_exc()
        return Response(
            content=json.dumps({"error": f"Failed to upload attachment: {str(e)}"}),
            status_code=500,
            media_type="application/json",
        )


# =============================================================================
# Unify Message
# =============================================================================


@app.post("/unify/message")
async def unify_message_webhook(request: Request):
    """
    Unify message webhook - handles internal message events.

    Accepts an optional 'attachments' array in the request body. Each attachment
    should be an object with: {"id": str, "filename": str, "url": str}
    (as returned by /unify/attachment).
    """
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
        contact_id = payload.get("contact_id")
        body = payload.get("body", "") or ""
        attachments = payload.get("attachments") or []
    else:
        form_data = await request.form()
        assistant_id_input = form_data.get("assistant_id", "")
        contact_id = form_data.get("contact_id")
        body = form_data.get("Body", "") or ""
        # Form data doesn't support attachments well, default to empty
        attachments = []

    if not assistant_id_input:
        print("Assistant ID is required")
        return Response(status_code=400)

    if contact_id is None:
        print("contact_id is required for unify_message")
        return Response(status_code=400, content="contact_id is required")

    # Validate attachment count limit
    if len(attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
        print(
            f"Too many attachments: {len(attachments)} exceeds limit of {MAX_ATTACHMENTS_PER_MESSAGE}"
        )
        return Response(
            status_code=400,
            content=f"Maximum {MAX_ATTACHMENTS_PER_MESSAGE} attachments per message allowed",
        )

    # Validate attachments format and preserve full metadata
    validated_attachments = []
    for att in attachments:
        if (
            isinstance(att, dict)
            and att.get("id")
            and att.get("filename")
            and (att.get("url") or att.get("gs_url"))  # Accept either URL type
        ):
            # Build validated attachment with all available metadata
            validated_att = {
                "id": str(att["id"]),
                "filename": str(att["filename"]),
                "url": str(att.get("url", "")),
            }
            # Include additional metadata if provided
            if att.get("gs_url"):
                validated_att["gs_url"] = str(att["gs_url"])
            if att.get("content_type"):
                validated_att["content_type"] = str(att["content_type"])
            if att.get("size_bytes") is not None:
                validated_att["size_bytes"] = int(att["size_bytes"])

            validated_attachments.append(validated_att)
        else:
            print(f"Skipping invalid attachment: {att}")

    attachment_info = (
        f" with {len(validated_attachments)} attachment(s)"
        if validated_attachments
        else ""
    )
    print(
        f"Received unify_message message for assistant_id={assistant_id_input}{attachment_info} with body: {body}",
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
    topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)
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
                        "attachments": validated_attachments,
                    },
                },
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


@app.post("/unify/meet")
async def unify_meet_webhook(request: Request):
    """Unify meet webhook - handles internal meet events."""
    print("unify_meet_webhook function started")

    # optional auth via admin key
    shared_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    auth_header = request.headers.get("Authorization", "")
    if shared_key and auth_header != f"Bearer {shared_key}":
        print("Unauthorized unify_meet request")
        return Response(status_code=401)

    # accept JSON or form payloads
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form_data = await request.form()
        payload = dict(form_data)

    livekit_agent_name = payload.get("livekit_agent_name", "")
    room_name = payload.get("room_name", "")
    if not livekit_agent_name or not room_name:
        print("livekit_agent_name and room_name are required")
        return Response(status_code=400)

    assistant_id_input = payload.get("assistant_id", "")
    if not assistant_id_input:
        print("assistant_id is required")
        return Response(status_code=400)

    print(
        f"Received unify_meet for assistant_id={assistant_id_input} room={room_name} livekit_agent_name={livekit_agent_name}",
    )

    # shared context
    context = build_webhook_context(
        channel="unify_meet",
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
    topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)
    print(f"Publishing unify_meet to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "unify_meet",
                    "event": {
                        "contacts": contacts,
                        "assistant_id": assistant_id,
                        "livekit_room": room_name,
                        "livekit_agent_name": livekit_agent_name,
                        "timestamp": int(time.time() * 1000),
                    },
                },
            ).encode("utf-8"),
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            print(f"Message ID: {message_id}")
        print("unify_meet message published to Pub/Sub successfully")
    except Exception as e:
        print(f"Error publishing unify_meet to Pub/Sub: {str(e)}")
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
        f"Received unity_system_event for event_type={event_type} with message={message}",
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
    topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)
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
                },
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


@app.post("/unity/pre-hire")
async def unity_pre_hire_webhook(request: Request):
    """Unity pre-hire webhook - logs chat history before hiring."""
    print("unity_pre_hire_webhook function started")

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
        f"Received log_pre_hire_chats for assistant_id={assistant_id_input} with {len(body)} messages",
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
    topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)
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
                },
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
    to the assistant_update topic. If no job is running, starts one first.
    """
    print("assistant_update_webhook function started")

    try:
        form_data = await request.form()
        assistant_id = form_data.get("assistant_id")
        print(f"Received assistant_id: {assistant_id}")

        # Use build_webhook_context to handle job startup if needed
        context = build_webhook_context(
            channel="assistant_update",
            destination="",
            sender="",
            assistant_id=assistant_id,
            validate_contact=False,
            ensure_job=True,
        )
        assistant_data = context["assistant"]
        print(
            f"Job running: {context['is_job_running']}, job started: {context['job_started']}"
        )

        # Prepare assistant_data for the PubSub message
        assistant_first_name = assistant_data["assistant_first_name"]
        assistant_surname = assistant_data["assistant_surname"]
        assistant_data["assistant_name"] = f"{assistant_first_name} {assistant_surname}"
        assistant_data.pop("assistant_first_name")
        assistant_data.pop("assistant_surname")
        assistant_data.pop("assistant_whatsapp_number")

        # Job is running, publish to assistant topic
        pubsub_client = pubsub_v1.PublisherClient()
        topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
        topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)

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
                },
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

        # extract Gmail notification details (mailbox address being watched)
        assistant_email_address = notification["emailAddress"]
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
            subject=assistant_email_address,
        )
        gmail_service = build("gmail", "v1", credentials=gmail_creds)

        # process the history and thread
        print(
            f"assistant_email_address: {assistant_email_address}, history_id: {history_id}",
        )
        thread_id, email_id, last_message, gmail_message_id = get_thread_id(
            assistant_email_address,
            history_id,
            gmail_service,
        )
        print(
            f"thread_id: {thread_id}, email_id: {email_id}, last_message: {last_message}",
        )
        if not thread_id:
            print(f"No new conversations found for user {assistant_email_address}")
            return Response(content="No new conversations", status_code=200)

        from_email = last_message["sender"].split("<")[1].split(">")[0]
        print(f"from_email: {from_email}")

        # shared context
        context = build_webhook_context("email", assistant_email_address, from_email)
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

        print(f"Successfully processed conversation for user {assistant_email_address}")
        publish_gmail_thread_id(
            assistant_id,
            user_id,
            thread_id,
            email_id,
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
async def outlook_notification_processor(request: Request):
    """
    Webhook endpoint to receive Microsoft Graph change notifications.
    Processes Outlook email notifications similar to Gmail notification processor.
    """
    try:
        notification = await request.json()

        # Parse clientState - format is "{secret}::{email}"
        # This contains the assistant email encoded when the subscription was created
        client_state = notification.get("clientState", "")
        expected_secret = os.getenv("OUTLOOK_WEBHOOK_SECRET", "unify-outlook-webhook")

        # Validate and extract email from clientState
        if "::" in client_state:
            secret_part, assistant_email_address = client_state.split("::", 1)
            if secret_part != expected_secret:
                print(f"Invalid clientState secret: {secret_part}")
                return Response(status_code=200)
        else:
            # Legacy format - just the secret, no email encoded
            print(f"Legacy clientState format (no email encoded): {client_state}")
            return Response(status_code=200)

        # Parse resource path to get message ID
        resource = notification.get("resource", "")
        if "/Messages/" not in resource:
            return Response(status_code=200)

        parts = resource.split("/")
        try:
            messages_index = parts.index("Messages") + 1
            email_id = parts[messages_index]
        except (ValueError, IndexError) as e:
            print(f"Could not parse message ID from resource path '{resource}': {e}")
            return Response(status_code=200)

        print(
            f"assistant_email_address: {assistant_email_address}, email_id: {email_id}",
        )

        # Get assistant data, secrets, and contacts in one call
        context = build_webhook_context(
            channel="email",
            destination=assistant_email_address,
            sender="",  # Unknown until we fetch the message
            validate_contact=False,  # We'll validate after fetching message
            ensure_job=False,  # We'll start job after validation
        )
        assistant_data = context["assistant"]
        if not assistant_data or not assistant_data.get("assistant_id"):
            print(f"Assistant not found for {assistant_email_address}")
            return Response(status_code=200)

        assistant_id = assistant_data["assistant_id"]
        user_id = assistant_data["user_id"]
        api_key = assistant_data["api_key"]

        # Get access token from secrets
        secrets = assistant_data.get("secrets", {})
        access_token = secrets.get("MICROSOFT_ACCESS_TOKEN")
        if not access_token:
            print(f"No Microsoft access token for {assistant_email_address}")
            return Response(status_code=200)

        # Create Graph client from token
        graph_client = get_graph_client_from_token(access_token)

        # Fetch message details to get the actual sender
        conversation_id, email_id, last_message = await get_outlook_thread_id(
            email_id,
            graph_client,
        )
        print(
            f"conversation_id: {conversation_id}, email_id: {email_id}, last_message: {last_message}",
        )

        if not conversation_id:
            print(f"No new conversations found for user {assistant_email_address}")
            return Response(status_code=200)

        from_email = last_message["sender"]
        print(f"from_email: {from_email}")

        # Validate contact now that we have the sender
        user_name = assistant_data["user_name"]
        assistant_first_name = assistant_data["assistant_first_name"]
        assistant_surname = assistant_data["assistant_surname"]
        contacts, is_valid_contact = check_valid_contact(
            email_address=from_email,
            medium="email",
            assistant_context=f"{user_name.replace(' ', '')}/{assistant_first_name}{assistant_surname}",
            api_key=api_key,
            user_number=assistant_data.get("user_number", ""),
            user_whatsapp_number=assistant_data.get("user_whatsapp_number", ""),
            user_email=assistant_data.get("user_email", ""),
            assistant_data=assistant_data,
        )

        if not is_valid_contact:
            error_message = (
                "This email address is no longer active. Please visit "
                "console.unify.ai to view your assistant details."
            )
            return Response(content=error_message, status_code=500)

        # Start job if not already running
        is_running = is_job_running(user_id, assistant_id)
        is_default = "default" in assistant_id
        if not is_running and not is_default:
            start_unity_job(assistant_data, "email")
            create_job(assistant_id)
            is_running = True

        print(f"Job running: {is_running}")

        print(f"Successfully processed conversation for user {assistant_email_address}")
        publish_outlook_thread_id(
            assistant_id,
            user_id,
            conversation_id,
            email_id,
            last_message,
            contacts,
        )
        return Response(content="OK", status_code=200)

    except Exception as e:
        error_message = f"Error processing notification: {str(e)}"
        traceback.print_exc()
        print(error_message)
        return Response(content=error_message, status_code=500)


# =============================================================================
# Microsoft Adapters
# =============================================================================


@app.post("/chat/teams")
async def teams_notification_processor(request: Request):
    """
    Process Teams notifications routed from /microsoft/router.
    Handles both chat messages (DMs, group chats) and channel messages.
    """
    try:
        notification = await request.json()
        print(f"Received Teams notification: {notification.get('resource', '')}")

        # Validate client state and extract assistant email
        client_state = notification.get("clientState", "")
        expected_secret = os.getenv("TEAMS_WEBHOOK_SECRET", "unify-teams-webhook")
        parts = client_state.split("::")
        if len(parts) < 2 or parts[0] != expected_secret:
            print(f"Invalid clientState format")
            return Response(status_code=200)

        assistant_email = parts[1]
        resource = notification.get("resource", "")
        resource_data = notification.get("resourceData", {})

        # Determine message type from resource path
        is_channel_message = (
            "teams(" in resource.lower() and "channels(" in resource.lower()
        )
        is_reply = "replies(" in resource.lower() or "/replies/" in resource.lower()
        msg_type = "channel" if is_channel_message else "chat"

        # Extract IDs
        if is_reply:
            message_id = resource_data.get("id") or parse_teams_resource_id(
                resource,
                "replies",
            )
            parent_message_id = parse_teams_resource_id(resource, "messages")
        else:
            message_id = resource_data.get("id") or parse_teams_resource_id(
                resource,
                "messages",
            )
            parent_message_id = None

        if is_channel_message:
            team_id = parse_teams_resource_id(resource, "teams")
            channel_id = parse_teams_resource_id(resource, "channels")
            chat_id = None
            if not team_id or not channel_id or not message_id:
                print(
                    f"Missing IDs for channel message: team={team_id}, channel={channel_id}, message={message_id}",
                )
                return Response(status_code=200)
            print(
                f"assistant_email: {assistant_email}, team_id: {team_id}, channel_id: {channel_id}, message_id: {message_id}",
            )
        else:
            chat_id = resource_data.get("chatId") or parse_teams_resource_id(
                resource,
                "chats",
            )
            team_id = channel_id = None
            if not chat_id or not message_id:
                print(
                    f"Missing IDs for chat message: chat={chat_id}, message={message_id}",
                )
                return Response(status_code=200)
            print(
                f"assistant_email: {assistant_email}, chat_id: {chat_id}, message_id: {message_id}",
            )

        # Get assistant data
        context = build_webhook_context(
            channel="teams",
            destination=assistant_email,
            sender="",
            validate_contact=False,
            ensure_job=False,
        )
        assistant_data = context["assistant"]
        if not assistant_data or not assistant_data.get("assistant_id"):
            print(f"Assistant not found for {assistant_email}")
            return Response(status_code=200)

        assistant_id = assistant_data["assistant_id"]
        user_id = assistant_data["user_id"]
        api_key = assistant_data["api_key"]
        access_token = assistant_data.get("secrets", {}).get("MICROSOFT_ACCESS_TOKEN")
        if not access_token:
            print(f"No Microsoft access token for {assistant_email}")
            return Response(status_code=200)

        # Fetch message from Graph API
        async def graph_get(url: str) -> tuple[dict | None, int]:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30.0,
                )
            return resp.json() if resp.status_code == 200 else None, resp.status_code

        message_data = None
        if is_channel_message:
            encoded_channel_id = quote(channel_id, safe="")
            if is_reply and parent_message_id:
                url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{encoded_channel_id}/messages/{parent_message_id}/replies?$top=50"
            else:
                url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{encoded_channel_id}/messages?$top=50"

            data, status = await graph_get(url)
            if not data:
                print(f"Failed to fetch {msg_type} messages (status={status})")
                return Response(status_code=200)

            # Find matching message in list
            for msg in data.get("value", []):
                if msg.get("id") == message_id:
                    message_data = msg
                    break
            if not message_data:
                print(f"Message {message_id} not found in list")
                return Response(status_code=200)
        else:
            url = f"https://graph.microsoft.com/v1.0/me/chats/{chat_id}/messages/{message_id}"
            message_data, status = await graph_get(url)
            if not message_data:
                print(f"Failed to fetch chat message (status={status})")
                return Response(status_code=200)

        # Extract sender info
        sender_user = message_data.get("from", {}).get("user", {})
        sender_name = sender_user.get("displayName", "Unknown")
        sender_id = sender_user.get("id")
        sender_email = sender_user.get("email") or sender_user.get("userPrincipalName")

        # Fetch email from user profile if not in message
        if not sender_email and sender_id:
            user_data, _ = await graph_get(
                f"https://graph.microsoft.com/v1.0/users/{sender_id}?$select=mail,userPrincipalName",
            )
            if user_data:
                sender_email = user_data.get("mail") or user_data.get(
                    "userPrincipalName",
                )
            if not sender_email:
                sender_email = f"{sender_id}@teams"

        print(f"from_email: {sender_email}, sender_name: {sender_name}")

        # Skip self-messages
        if sender_email and sender_email.lower() == assistant_email.lower():
            print(f"Skipping self-message from {sender_email}")
            return Response(status_code=200)

        # Validate contact
        user_name = assistant_data["user_name"]
        assistant_first_name = assistant_data["assistant_first_name"]
        assistant_surname = assistant_data["assistant_surname"]
        contacts, is_valid = check_valid_contact(
            email_address=sender_email,
            medium="teams",
            assistant_context=f"{user_name.replace(' ', '')}/{assistant_first_name}{assistant_surname}",
            api_key=api_key,
            user_number=assistant_data.get("user_number", ""),
            user_whatsapp_number=assistant_data.get("user_whatsapp_number", ""),
            user_email=assistant_data.get("user_email", ""),
            assistant_data=assistant_data,
        )
        if not is_valid:
            print(f"Invalid contact: {sender_email}")
            return Response(status_code=200)

        # Start job if needed
        is_running = is_job_running(user_id, assistant_id)
        is_default = assistant_id and "default" in assistant_id
        if not is_running and not is_default:
            start_unity_job(assistant_data, "teams")
            create_job(assistant_id)
            is_running = True
        print(f"Job running: {is_running}")

        # Build event payload
        message_content = message_data.get("body", {}).get("content", "")
        message_content_type = message_data.get("body", {}).get("contentType", "text")
        subject = message_data.get("subject", "")

        event_data = {
            "contacts": contacts,
            "message_id": message_id,
            "sender": sender_email,
            "sender_name": sender_name,
            "sender_id": sender_id,
            "message": message_content,
            "content_type": message_content_type,
            "created_at": message_data.get("createdDateTime"),
            "assistant_email": assistant_email,
            "is_channel_message": is_channel_message,
            "timestamp": int(time.time() * 1000),
        }

        if is_channel_message:
            event_data.update(
                {
                    "team_id": team_id,
                    "channel_id": channel_id,
                    "is_reply": is_reply,
                    "parent_message_id": parent_message_id if is_reply else None,
                    "thread_id": parent_message_id if is_reply else message_id,
                    "post_subject": None if is_reply else subject,
                    "action": "new_channel_message",
                },
            )
        else:
            event_data.update({"chat_id": chat_id, "action": "new_message"})

        # Publish to Pub/Sub
        pubsub_client = pubsub_v1.PublisherClient()
        topic_name = f"unity-{assistant_id}" + ("" if not STAGING else "-staging")
        topic_path = pubsub_client.topic_path(os.getenv("GCP_PROJECT_ID"), topic_name)

        pubsub_message = {
            "thread": "teams_channel" if is_channel_message else "teams_chat",
            "event": event_data,
        }
        print(f"event_data: {event_data}")

        try:
            publish_future = pubsub_client.publish(
                topic_path,
                json.dumps(pubsub_message).encode("utf-8"),
            )
            publish_future.result(timeout=5)
        except Exception as e:
            print(f"Pub/Sub publish error: {e}")
            return Response(content=str(e), status_code=500)

        print(f"Successfully processed Teams {msg_type} message for {assistant_email}")
        return Response(content="OK", status_code=200)

    except Exception as e:
        error_message = f"Error processing Teams notification: {str(e)}"
        traceback.print_exc()
        print(error_message)
        return Response(content=error_message, status_code=500)


@app.post("/microsoft/router")
async def microsoft_router(request: Request):
    """
    Router for Microsoft Graph webhook notifications.
    Returns 200 immediately to satisfy Microsoft's 3-second timeout,
    then routes notifications to appropriate processors via HTTP.
    """
    print("microsoft_router function started")

    # Handle validation token (required for subscription setup)
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        print("returning validation token")
        return Response(content=validation_token, media_type="text/plain")

    # Parse notifications
    body = await request.body()
    json_body = json.loads(body)
    notifications = json_body.get("value", [])
    print(f"routing {len(notifications)} notification(s)")

    # Route each notification to appropriate processor
    adapters_url = os.getenv("UNITY_ADAPTERS_URL", "http://localhost:8001")

    async with httpx.AsyncClient() as client:
        for notification in notifications:
            resource = notification.get("resource", "")

            # Route based on resource type
            if "/Messages/" in resource and "/chats/" not in resource.lower():
                # Outlook email messages (users/{id}/mailFolders/inbox/messages)
                target = f"{adapters_url}/email/outlook"
            elif (
                "/chats/" in resource.lower()
                or "getAllMessages" in resource
                or ("teams(" in resource.lower() and "channels(" in resource.lower())
            ):
                # Teams messages - both chat and channel go to same handler
                # Channel notifications come as: teams('id')/channels('id')/messages('id')
                target = f"{adapters_url}/chat/teams"
            else:
                print(f"unknown resource type: {resource}")
                continue

            print(f"routing to {target}")
            try:
                await client.post(target, json=notification, timeout=1.0)
            except Exception as e:
                # Log but don't fail - the request was sent
                print(f"request sent (may have timed out on response): {e}")

    print("returning 200 OK")
    return Response(content="OK", status_code=200)


@app.get("/microsoft/auth/callback")
async def microsoft_oauth_callback(request: Request):
    """
    OAuth callback - Microsoft redirects here after user authorizes.
    See docs/MICROSOFT_OAUTH_SETUP_GUIDE.md for full setup instructions.

    State must contain:
    - assistant_email: The assistant's email (to look up credentials)
    - tenant_id: Azure AD tenant ID
    - client_id: Azure AD app client ID
    - redirect_after: (optional) URL to redirect to after success
    """
    # Get query params
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    error_description = request.query_params.get("error_description")

    if error:
        print(f"OAuth error: {error} - {error_description}")
        return Response(
            content=f"OAuth error: {error}: {error_description}",
            status_code=400,
        )

    if not code:
        return Response(content="Missing authorization code", status_code=400)

    # Decode state
    tenant_id = None
    client_id = None
    redirect_after = None
    assistant_email = None

    if state:
        try:
            state_data = json.loads(base64.b64decode(state).decode())
            tenant_id = state_data.get("tenant_id")
            client_id = state_data.get("client_id")
            redirect_after = state_data.get("redirect_after")
            assistant_email = state_data.get("assistant_email")
        except Exception:
            return Response(content="Invalid state parameter", status_code=400)

    if not tenant_id or not client_id:
        return Response(
            content="Missing tenant_id or client_id in state",
            status_code=400,
        )

    if not assistant_email:
        return Response(content="Missing assistant_email in state", status_code=400)

    # Get assistant data (including secrets) from orchestra
    assistant = get_assistant(email_address=assistant_email)
    if not assistant or not assistant.get("assistant_id"):
        return Response(
            content=f"Assistant not found for email: {assistant_email}",
            status_code=400,
        )

    secrets = assistant.get("secrets", {})
    client_secret = secrets.get("AZURE_CLIENT_SECRET")

    if not client_secret:
        return Response(
            content="AZURE_CLIENT_SECRET not found in assistant secrets. Add it to the assistant configuration.",
            status_code=400,
        )

    redirect_uri = os.getenv("UNITY_ADAPTERS_URL", "") + "/microsoft/auth/callback"

    # Exchange code for tokens
    try:
        tokens = await exchange_microsoft_code_for_tokens(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except Exception as e:
        print(f"Token exchange failed: {e}")
        return Response(content=f"Token exchange failed: {e}", status_code=400)

    # Get user email from token (should match assistant_email)
    try:
        print(f"tokens: {tokens}")
        user_info = await get_microsoft_user_info(tokens["access_token"])
        user_email = user_info.get("mail") or user_info.get("userPrincipalName")
    except Exception as e:
        print(f"Failed to get user info: {e}")
        return Response(content=f"Failed to get user info: {e}", status_code=400)

    if not user_email:
        return Response(
            content="Could not determine user email from token",
            status_code=400,
        )

    # Store tokens as assistant secrets
    assistant_id = assistant["assistant_id"]
    api_key = assistant["api_key"]
    stored = await store_microsoft_tokens(
        assistant_id=assistant_id,
        old_secrets=secrets,
        new_secrets=tokens,
        api_key=api_key,
    )

    print(
        f"OAuth complete for {user_email} (assistant: {assistant_email}, id: {assistant_id}), stored={stored}",
    )

    # Redirect to success page or return JSON
    if redirect_after:
        sep = "&" if "?" in redirect_after else "?"
        return RedirectResponse(
            f"{redirect_after}{sep}success=true&user_email={user_email}",
        )

    return Response(
        content=json.dumps(
            {"success": True, "user_email": user_email, "stored": stored},
        ),
        media_type="application/json",
    )


# =============================================================================
# Scheduled Endpoints
# =============================================================================


@app.post("/scheduled/email-watches")
async def scheduled_email_watches(request: Request):
    """
    Cloud Run endpoint that renews email watches for all assistants.
    Automatically detects provider based on secrets:
    - If MICROSOFT_ACCESS_TOKEN exists → Outlook watch
    - Otherwise → Gmail watch
    """
    payload = await request.json()
    test = payload.get("test", False)

    admin_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    if not admin_key:
        return Response(content="ORCHESTRA_ADMIN_KEY not configured", status_code=500)

    # Fetch all assistants with their secrets
    if not test:
        try:
            response = requests.get(
                f"{ORCHESTRA_URL}/admin/assistant",
                headers={"Authorization": f"Bearer {admin_key}"},
            )
            if response.status_code != 200:
                return Response(
                    content=f"Failed to get assistants: {response.text}",
                    status_code=500,
                )
            all_assistants = response.json().get("info", [])
        except Exception as e:
            print(f"Failed to get assistants: {e}")
            return Response(content=f"Failed to get assistants: {e}", status_code=500)
    else:
        all_assistants = [{"email": "default-test-assistant@unify.ai", "secrets": {}}]

    print(f"Processing {len(all_assistants)} assistants for email watch renewal")

    results = {"gmail": [], "outlook": []}

    for assistant in all_assistants:
        email = assistant.get("email")
        if not email:
            continue

        secrets = assistant.get("secrets", {})
        has_ms_token = bool(secrets.get("MICROSOFT_ACCESS_TOKEN"))

        try:
            if has_ms_token:
                # Use Outlook watch
                watch_response = requests.post(
                    f"{COMMS_URL}/outlook/watch",
                    json={"primary_email": email},
                    headers={"Authorization": f"Bearer {admin_key}"},
                    timeout=30,
                )
                result = (
                    watch_response.json()
                    if watch_response.status_code == 200
                    else {
                        "success": False,
                        "error": watch_response.text,
                    }
                )
                results["outlook"].append({"email": email, **result})
                print(f"Outlook watch for {email}: {result.get('success', False)}")
            else:
                # Use Gmail watch
                watch_response = requests.post(
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
                    timeout=30,
                )
                result = (
                    watch_response.json()
                    if watch_response.status_code == 200
                    else {
                        "success": False,
                        "error": watch_response.text,
                    }
                )
                results["gmail"].append({"email": email, **result})
                print(f"Gmail watch for {email}: {result.get('success', False)}")

        except Exception as e:
            error_msg = f"Error renewing watch for {email}: {str(e)}"
            print(error_msg)
            provider = "outlook" if has_ms_token else "gmail"
            results[provider].append(
                {"email": email, "success": False, "error": error_msg},
            )

    # Renew policy assistant (Gmail-based, staging only)
    if STAGING and not test:
        try:
            response = requests.post(
                f"{COMMS_URL}/gmail/watch",
                json={
                    "primary_email": "mh-policies@unify.ai",
                    "topic_name": "intranet",
                },
                headers={"Authorization": f"Bearer {admin_key}"},
                timeout=30,
            ).json()
            results["gmail"].append({"email": "mh-policies@unify.ai", **response})
            print(f"Renewed policy assistant: {response}")
        except Exception as e:
            print(f"Error renewing policy assistant: {e}")

    print(
        f"Email watch renewal complete: {len(results['gmail'])} Gmail, {len(results['outlook'])} Outlook",
    )
    return results


@app.post("/scheduled/microsoft-tokens")
async def scheduled_microsoft_tokens(request: Request):
    """
    Cloud Run endpoint that refreshes Microsoft OAuth tokens for all assistants.
    Should be scheduled to run every 30-45 minutes to keep access tokens fresh.

    Fetches all assistants in a single call and only processes those with
    Microsoft tokens configured in their secrets.
    """
    payload = await request.json()
    test = payload.get("test", False)

    admin_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    if not admin_key:
        return Response(content="ORCHESTRA_ADMIN_KEY not configured", status_code=500)

    # Get all assistants in a single call (no params = all assistants)
    try:
        response = requests.get(
            f"{ORCHESTRA_URL}/admin/assistant",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        if response.status_code != 200:
            return Response(
                content=f"Failed to get assistants: {response.text}",
                status_code=500,
            )
        all_assistants = response.json().get("info", [])
    except Exception as e:
        print(f"Failed to get assistants: {e}")
        return Response(content=f"Failed to get assistants: {e}", status_code=500)

    print(f"Fetched {len(all_assistants)} assistants")
    results = {"refreshed": [], "failed": []}

    for assistant in all_assistants:
        email = assistant.get("email", "unknown")
        assistant_id = assistant.get("agent_id")

        # Skip test in non-test mode
        if test and email != "default-test-assistant@unify.ai":
            continue

        # Skip if no Microsoft tokens configured
        secrets = assistant.get("secrets", {})
        if not secrets:
            continue
        access_token = secrets.get("MICROSOFT_ACCESS_TOKEN")
        refresh_token = secrets.get("MICROSOFT_REFRESH_TOKEN")
        if not access_token or not refresh_token:
            continue

        # Get required credentials for refresh
        tenant_id = secrets.get("AZURE_TENANT_ID")
        client_id = secrets.get("AZURE_CLIENT_ID")
        client_secret = secrets.get("AZURE_CLIENT_SECRET")
        if not all([tenant_id, client_id, client_secret]):
            results["failed"].append(
                {"email": email, "error": "Missing Azure credentials"},
            )
            continue

        try:
            # Refresh the token
            token_resp = requests.post(
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    "scope": "https://graph.microsoft.com/.default offline_access",
                },
            )

            if token_resp.status_code != 200:
                results["failed"].append(
                    {
                        "email": email,
                        "error": f"Token refresh failed: {token_resp.text}",
                    },
                )
                continue

            new_tokens = token_resp.json()
            expires_at = (
                datetime.now(tz=timezone.utc)
                + timedelta(seconds=new_tokens.get("expires_in", 3600))
            ).isoformat()

            # Store updated tokens
            api_key = assistant.get("api_key")

            secrets_to_store = {
                "MICROSOFT_ACCESS_TOKEN": new_tokens["access_token"],
                "MICROSOFT_TOKEN_EXPIRES_AT": expires_at,
            }
            # Only update refresh token if a new one was issued
            if new_tokens.get("refresh_token"):
                secrets_to_store["MICROSOFT_REFRESH_TOKEN"] = new_tokens[
                    "refresh_token"
                ]

            for secret_name, secret_value in secrets_to_store.items():
                response = requests.put(
                    f"{ORCHESTRA_URL}/assistant/{assistant_id}/secret/{secret_name}",
                    json={"secret_value": secret_value},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if response.status_code != 200:
                    results["failed"].append(
                        {
                            "email": email,
                            "error": f"Failed to store secret: {response.text}",
                        },
                    )
                    continue

            results["refreshed"].append(email)
            print(f"Refreshed Microsoft token for {email}")

        except Exception as e:
            results["failed"].append({"email": email, "error": str(e)})
            print(f"Error refreshing token for {email}: {e}")

    print(
        f"Microsoft token refresh complete: "
        f"{len(results['refreshed'])} refreshed, {len(results['failed'])} failed",
    )

    return results


@app.post("/scheduled/teams-watches")
async def scheduled_teams_watches(request: Request):
    """
    Cloud Run endpoint that renews Teams chat AND channel subscriptions for all assistants.
    Teams subscriptions expire after 60 minutes, so this should run every 30-45 mins.
    Only processes assistants with MICROSOFT_ACCESS_TOKEN in their secrets.

    For chats: Creates/renews /me/chats/getAllMessages subscription
    For channels: Finds and renews any existing channel subscriptions
    """
    payload = await request.json()
    test = payload.get("test", False)

    admin_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    if not admin_key:
        return Response(content="ORCHESTRA_ADMIN_KEY not configured", status_code=500)

    # Fetch all assistants with their secrets
    if not test:
        try:
            response = requests.get(
                f"{ORCHESTRA_URL}/admin/assistant",
                headers={"Authorization": f"Bearer {admin_key}"},
            )
            if response.status_code != 200:
                return Response(
                    content=f"Failed to get assistants: {response.text}",
                    status_code=500,
                )
            all_assistants = response.json().get("info", [])
        except Exception as e:
            print(f"Failed to get assistants: {e}")
            return Response(content=f"Failed to get assistants: {e}", status_code=500)
    else:
        all_assistants = [
            {
                "email": "default-test-assistant@unify.ai",
                "secrets": {"MICROSOFT_ACCESS_TOKEN": "test"},
            },
        ]

    print(f"Processing {len(all_assistants)} assistants for Teams watch renewal")

    results = {
        "chats_renewed": [],
        "channels_renewed": [],
        "skipped": [],
        "failed": [],
    }

    for assistant in all_assistants:
        email = assistant.get("email")
        if not email:
            continue

        secrets = assistant.get("secrets", {})
        access_token = secrets.get("MICROSOFT_ACCESS_TOKEN")

        # Skip if no Microsoft token (not using Microsoft services)
        if not access_token:
            continue

        try:
            # 1. Renew Teams chat watch (DMs and group chats)
            watch_response = requests.post(
                f"{COMMS_URL}/teams/watch",
                json={"primary_email": email},
                headers={"Authorization": f"Bearer {admin_key}"},
                timeout=30,
            )
            result = (
                watch_response.json()
                if watch_response.status_code == 200
                else {"success": False, "error": watch_response.text}
            )
            if result.get("success"):
                results["chats_renewed"].append({"email": email, **result})
            else:
                results["failed"].append({"email": email, "type": "chat", **result})
            print(f"Teams chat watch for {email}: {result.get('success', False)}")

            # 2. Renew any existing channel subscriptions
            # List all subscriptions and renew those matching /teams/{id}/channels/{id}/messages
            async with httpx.AsyncClient() as client:
                subs_response = await client.get(
                    "https://graph.microsoft.com/v1.0/subscriptions",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=30.0,
                )

            print(f"Subscriptions response status code: {subs_response.status_code}")
            print(f"Subscriptions response text: {subs_response.text}")
            if subs_response.status_code == 200:
                subs_data = subs_response.json()
                for sub in subs_data.get("value", []):
                    resource = sub.get("resource", "")
                    # Check if this is a channel subscription
                    if "/teams/" in resource and "/channels/" in resource:
                        # Parse team_id and channel_id from resource
                        try:
                            parts = resource.split("/")
                            team_idx = parts.index("teams")
                            channel_idx = parts.index("channels")
                            team_id = parts[team_idx + 1]
                            channel_id = parts[channel_idx + 1]

                            # Renew channel subscription
                            channel_response = requests.post(
                                f"{COMMS_URL}/teams/watch-channel",
                                json={
                                    "primary_email": email,
                                    "team_id": team_id,
                                    "channel_id": channel_id,
                                },
                                headers={"Authorization": f"Bearer {admin_key}"},
                                timeout=30,
                            )
                            ch_result = (
                                channel_response.json()
                                if channel_response.status_code == 200
                                else {"success": False, "error": channel_response.text}
                            )
                            if ch_result.get("success"):
                                results["channels_renewed"].append(
                                    {"email": email, **ch_result},
                                )
                            else:
                                results["failed"].append(
                                    {"email": email, "type": "channel", **ch_result},
                                )
                            print(
                                f"Teams channel watch for {email} ({team_id}/{channel_id}): "
                                f"{ch_result.get('success', False)}",
                            )
                        except (ValueError, IndexError) as e:
                            print(f"Could not parse channel subscription: {resource}")

        except requests.exceptions.Timeout:
            # Timeout is not a failure - the request was sent and may succeed
            print(f"Teams watch for {email}: timed out (request may still succeed)")
            results["channels_renewed"].append({"email": email, "status": "timeout"})
        except Exception as e:
            error_msg = f"Error renewing Teams watch for {email}: {str(e)}"
            print(error_msg)
            traceback.print_exc()
            results["failed"].append(
                {"email": email, "success": False, "error": error_msg},
            )

    print(
        f"Teams watch renewal complete: "
        f"{len(results['chats_renewed'])} chats, "
        f"{len(results['channels_renewed'])} channels, "
        f"{len(results['failed'])} failed",
    )
    return results


@app.post("/scheduled/jobs/create")
async def scheduled_jobs_create(request: Request):
    """Cloud Run endpoint that creates a new idle job."""
    return Response(content="Not implemented", status_code=200)
    if not STAGING:
        return Response(
            content="Production job creation is not enabled",
            status_code=200,
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
        f"{COMMS_URL}/infra/job/create",
        data={"image": image},
        headers=headers,
    )
    return response.json()


@app.post("/scheduled/jobs/cleanup")
async def scheduled_jobs_cleanup(request: Request):
    """Cloud Run endpoint that cleans idle jobs that have been around for >24 hours."""
    return Response(content="Not implemented", status_code=200)
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
            "Ping received - keeping conversation manager alive" in logs
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
    print("    - POST /twilio/sms")
    print("    - POST /twilio/whatsapp")
    print("  Unify:")
    print("    - POST /unify/message")
    print("    - POST /unify/meet")
    print("  Unity:")
    print("    - POST /unity/system-event")
    print("    - POST /unity/pre-hire")
    print("  Assistant:")
    print("    - POST /assistant/wakeup")
    print("    - POST /assistant/update")
    print("  Email:")
    print("    - POST /email/gmail")
    print("    - POST /email/outlook")
    print("  Microsoft:")
    print("    - POST /microsoft/router")
    print("    - GET  /microsoft/auth/callback")
    print("  Scheduled:")
    print("    - POST /scheduled/email-watches")
    print("    - POST /scheduled/jobs/create")
    print("    - POST /scheduled/jobs/cleanup")
    print("Server running at: http://localhost:8080")

    uvicorn.run("adapters.main:app", host="0.0.0.0", port=8080, reload=True)
