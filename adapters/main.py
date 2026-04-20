import asyncio
import json
import base64
import logging
import mimetypes
import time
import uuid
from dotenv import load_dotenv
import os
import requests
import httpx
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Optional
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from google.cloud import storage
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import VoiceResponse

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
logger = logging.getLogger(__name__)


def _redact_phone(number: str) -> str:
    if len(number) < 4:
        return "***"
    return f"***{number[-4:]}"


def _redact_email(email: str) -> str:
    if "@" in email:
        return f"***@{email.split('@', 1)[1]}"
    return "***"


from common.metrics import setup_metrics

from common.livekit import (
    ensure_phone_dispatch_rule,
    make_room_name,
    make_sip_uri,
    start_room_egress,
    verify_livekit_webhook,
)

from common.oauth import OAuthStateError, verify_oauth_state
from common.settings import SETTINGS

# Canonical source: communication.infra.vm_config.SUPPORTED_POOL_VM_TYPES
# Duplicated here because the adapters container does not include the
# communication package at runtime.
SUPPORTED_POOL_VM_TYPES: tuple[str, ...] = ("ubuntu", "windows")

from .helpers import (
    cleanup_idle_pool,
    get_admin_graph_bearer_token,
    get_outlook_graph_client,
    replenish_idle_pool,
    add_user_to_conference,
    build_webhook_context,
    check_valid_contact,
    create_conference_response,
    dispatch_livekit_agent,
    dispatch_unity_start_intent,
    expire_all_stale_jobs,
    get_assistant,
    get_outlook_thread_id,
    get_pubsub_client,
    get_thread_id,
    get_twilio_wa_client,
    parse_teams_resource_id,
    publish_gmail_thread_id,
    publish_outlook_thread_id,
    resolve_whatsapp_route,
    start_unity_job,
    uses_local_unity_runtime,
)
from common.google_oauth import (
    exchange_google_code_for_tokens,
    get_google_user_info,
    refresh_google_tokens,
    store_google_tokens,
)
from common.microsoft_oauth import (
    exchange_microsoft_code_for_tokens,
    get_microsoft_user_info,
    store_microsoft_tokens,
)

load_dotenv()
app = FastAPI(
    title="Unity Adapters",
    description="Webhook adapters for Twilio, Gmail, and internal services",
    version="1.0.0",
)
setup_metrics(app, service_name="adapters")


# =============================================================================
# Twilio Signature Validation
# =============================================================================

_twilio_validator = None
_twilio_wa_validator = None


def _get_twilio_validator() -> RequestValidator:
    global _twilio_validator
    if _twilio_validator is None:
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        if not auth_token:
            raise RuntimeError("TWILIO_AUTH_TOKEN is required but not set")
        _twilio_validator = RequestValidator(auth_token)
    return _twilio_validator


def _get_twilio_wa_validator() -> RequestValidator:
    global _twilio_wa_validator
    if _twilio_wa_validator is None:
        auth_token = os.environ.get("TWILIO_WA_AUTH_TOKEN")
        if not auth_token:
            raise RuntimeError("TWILIO_WA_AUTH_TOKEN is required but not set")
        _twilio_wa_validator = RequestValidator(auth_token)
    return _twilio_wa_validator


async def _validate_twilio_sig(request: Request, validator: RequestValidator):
    """Shared logic for Twilio signature validation.

    Cloud Run proxies rewrite the Host header, so ``str(request.url)``
    returns an internal URL that differs from the public URL Twilio signed
    against. Reconstruct the original URL from forwarded headers.
    """
    signature = request.headers.get("X-Twilio-Signature", "")
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.headers.get("Host", ""))
    url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    form_data = await request.form()
    params = {k: v for k, v in form_data.items()}
    if not validator.validate(url, params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


async def validate_twilio_signature(request: Request):
    await _validate_twilio_sig(request, _get_twilio_validator())


async def validate_twilio_wa_signature(request: Request):
    await _validate_twilio_sig(request, _get_twilio_wa_validator())


# =============================================================================
# Admin Key Auth for Adapters
# =============================================================================


async def require_admin_key(request: Request):
    """FastAPI dependency that requires a valid admin key."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing admin key")
    import secrets as _secrets

    token = auth_header[len("Bearer ") :]
    admin_key = SETTINGS.orchestra_admin_key
    if not admin_key or not _secrets.compare_digest(token, admin_key):
        raise HTTPException(status_code=403, detail="Invalid admin key")


# =============================================================================
# Rate Limiting Middleware
# =============================================================================

import time as _time
from collections import defaultdict
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Limits requests per IP to prevent abuse of webhook endpoints."""

    def __init__(self, app_instance, max_requests: int = 120, window_seconds: int = 60):
        super().__init__(app_instance)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = _time.monotonic()
        window_start = now - self.window_seconds
        timestamps = self._requests[client_ip]
        self._requests[client_ip] = [t for t in timestamps if t > window_start]
        if len(self._requests[client_ip]) >= self.max_requests:
            return Response(
                content=json.dumps({"detail": "Rate limit exceeded"}),
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(self.window_seconds)},
            )
        self._requests[client_ip].append(now)
        return await call_next(request)


app.add_middleware(RateLimitMiddleware, max_requests=120, window_seconds=60)


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


@app.post("/twilio/call", dependencies=[Depends(validate_twilio_signature)])
async def twilio_call_webhook(request: Request):
    """Phone call webhook endpoint - handles incoming Twilio voice calls."""
    logger.info("twilio_call_webhook function started")
    form_data = await request.form()

    # get twilio number and caller number
    to_number = form_data.get("To", "")
    from_number = form_data.get("From", "")
    twilio_number = to_number or ""
    caller_number = from_number or ""
    logger.info(
        f"Received call from {_redact_phone(caller_number)} to {_redact_phone(twilio_number)}",
    )

    # shared context
    context = await asyncio.to_thread(
        build_webhook_context,
        "phone",
        to_number,
        from_number,
    )
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]

    if not context["is_valid_contact"]:
        resp_user = VoiceResponse()
        resp_user.say(
            "This number is no longer active. Please visit "
            "console.unify.ai to view your assistant details.",
        )
        return Response(content=str(resp_user), media_type="text/xml")

    logger.info(
        "Activation intent scheduled (legacy is_job_running flag): %s",
        context["is_job_running"],
    )

    # conference name and SIP URI
    date_time = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    conference_name = f"Unity_{twilio_number[1:]}_{date_time}"
    room_name = make_room_name(assistant_id, "phone")
    sip_uri = make_sip_uri(twilio_number)
    logger.info(f"Setting up conference {conference_name}")
    logger.info(f"LiveKit room will be: {room_name}")

    await ensure_phone_dispatch_rule(twilio_number, room_name)

    # publish to Pub/Sub
    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    logger.info(f"Publishing call to Pub/Sub at path: {topic_path}")
    try:
        pubsub_message = {
            "thread": "call",
            "publish_timestamp": time.time(),
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
            thread="inbound",
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            logger.info(f"Message ID: {message_id}")
        logger.info("Call published to Pub/Sub successfully")
    except Exception as e:
        logger.error(f"Error publishing to Pub/Sub: {e}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    # Conference setup
    try:
        resp_user = create_conference_response(conference_name)
        logger.info(f"Conference response: {resp_user.to_xml()}")
        if resp_user:
            logger.info("Conference response created successfully")
        else:
            logger.error("Failed to create conference response")
            return Response(content="Error creating conference", status_code=500)

        call_sid = add_user_to_conference(conference_name, caller_number, sip_uri)
        if call_sid:
            logger.info(f"User added to conference successfully. Call SID: {call_sid}")
        else:
            logger.error("Failed to add user to conference")
            return Response(content="Error adding user to conference", status_code=500)

        logger.info(f"Assistant ID: {assistant_id}")
        if assistant_id == "default-assistant":
            logger.info(f"Dispatching LiveKit agent {room_name}")
            dispatch_livekit_agent(room_name)

        logger.info("Conference setup completed")
    except Exception as e:
        logger.error(f"Error during conference setup: {e}")
        return Response(content="Error setting up conference", status_code=500)

    # Start LiveKit Egress recording on the room (fire-and-forget).
    try:
        user_id = context["assistant"]["user_id"]
        await start_room_egress(room_name, assistant_id, user_id)
    except Exception as e:
        logger.error(f"[Egress] Non-fatal: failed to start egress for call: {e}")

    logger.info("Returning TwiML response")
    return Response(content=str(resp_user), media_type="text/xml")


@app.post("/twilio/call-status", dependencies=[Depends(validate_twilio_signature)])
async def twilio_call_status_webhook(request: Request):
    """Phone call status webhook - handles Twilio call status updates."""
    form_data = await request.form()
    call_status = form_data.get("CallStatus")
    assistant_number = form_data.get("From")
    user_number = form_data.get("To")
    logger.info(f"twilio_call_status_webhook function started: {call_status}")
    logger.info(
        f"User {_redact_phone(user_number)} called by {_redact_phone(assistant_number)}",
    )

    # Handle call answered (in-progress) or not answered (no-answer, busy, canceled, failed)
    if call_status in ("in-progress", "no-answer", "busy", "canceled", "failed"):
        # get assistant data
        context = await asyncio.to_thread(
            build_webhook_context,
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
        pubsub_client = get_pubsub_client()
        topic_name = SETTINGS.assistant_topic(assistant_id)
        topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
        logger.info(f"Publishing {thread} to Pub/Sub at path: {topic_path}")
        try:
            publish_future = pubsub_client.publish(
                topic_path,
                json.dumps(
                    {
                        "thread": thread,
                        "publish_timestamp": time.time(),
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
                thread="inbound",
            )
            if "test" in assistant_id:
                status_id = publish_future.result(timeout=10)
                logger.info(f"Message ID: {status_id}")
            logger.info(f"{thread} published to Pub/Sub successfully")
        except Exception as e:
            logger.error(f"Error publishing to Pub/Sub: {e}")

    return Response(status_code=200)


# =============================================================================
# LiveKit Webhooks
# =============================================================================
@app.post("/livekit/recording-complete")
async def livekit_recording_complete(request: Request):
    """Handle LiveKit Egress completion webhooks for call/meet recordings.

    LiveKit Egress uploads the recording directly to GCS. This adapter
    verifies the webhook, ensures the assistant's Unity container is
    running, and publishes a recording_ready Pub/Sub event so Unity can
    link the recording URL to the transcript exchange.
    """
    body = (await request.body()).decode()
    auth_token = request.headers.get("Authorization", "")

    try:
        event = verify_livekit_webhook(body, auth_token)
    except Exception as e:
        logger.error(f"[Recording] Webhook verification failed: {e}")
        return Response(status_code=401)

    if event.event != "egress_ended":
        return Response(status_code=200)

    egress_info = event.egress_info
    logger.info(
        f"[Recording] Egress {egress_info.egress_id} ended for room "
        f"'{egress_info.room_name}' status={egress_info.status}",
    )

    if not egress_info.file_results:
        logger.info("[Recording] No file results in egress info, skipping")
        return Response(status_code=200)

    assistant_id = request.query_params.get("assistant_id", "")
    user_id = request.query_params.get("user_id", "")
    room_name = request.query_params.get("room_name", egress_info.room_name)
    logger.info(f"Assistant ID: {assistant_id}")
    logger.info(f"User ID: {user_id}")
    logger.info(f"Room Name: {room_name}")

    if not assistant_id:
        logger.info("[Recording] Missing assistant_id, cannot route event")
        return Response(status_code=200)

    # Ensure the assistant's container is running so the Pub/Sub message
    # has a receiver. Uses validate_contact=False (this is an internal
    # infrastructure event, not a user-initiated contact).
    try:
        context = await asyncio.to_thread(
            build_webhook_context,
            "recording",
            destination="",
            sender="",
            assistant_id=assistant_id,
            validate_contact=False,
            ensure_job=True,
        )
        logger.info(
            "[Recording] Activation intent scheduled "
            "(legacy is_job_running flag): %s",
            context["is_job_running"],
        )
    except Exception as e:
        logger.error(
            f"[Recording] Failed to ensure job for assistant {assistant_id}: {e}",
        )

    # Construct GCS public URL and publish.
    file_result = egress_info.file_results[0]
    gcs_bucket = os.getenv("LIVEKIT_EGRESS_GCS_BUCKET", "unity-call-recordings")
    recording_url = (
        f"https://storage.googleapis.com/{gcs_bucket}/{file_result.filename}"
    )

    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    logger.info(f"Publishing recording_ready to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "recording_ready",
                    "publish_timestamp": time.time(),
                    "event": {
                        "assistant_id": str(assistant_id),
                        "user_id": str(user_id),
                        "conference_name": room_name,
                        "recording_url": recording_url,
                    },
                },
            ).encode("utf-8"),
            thread="inbound",
        )
        if "test" in str(assistant_id):
            publish_future.result(timeout=10)
        logger.info(
            f"[Recording] Published recording_ready for assistant {assistant_id}, "
            f"room '{room_name}', size={file_result.size} bytes",
        )
    except Exception as e:
        logger.error(f"[Recording] Error publishing to Pub/Sub: {e}")
        return Response(status_code=500)

    return {"success": True}


@app.post("/twilio/sms", dependencies=[Depends(validate_twilio_signature)])
async def twilio_sms_webhook(request: Request):
    """SMS webhook endpoint - handles incoming Twilio SMS messages."""
    logger.info("twilio_sms_webhook function started")
    form_data = await request.form()

    # get twilio number and caller number
    to_number = form_data.get("To", "") or ""
    from_number = form_data.get("From", "") or ""
    body = form_data.get("Body", "") or ""
    logger.info(
        f"Received SMS from {_redact_phone(from_number)} to {_redact_phone(to_number)}",
    )

    # shared context
    context = await asyncio.to_thread(
        build_webhook_context,
        "msg",
        to_number,
        from_number,
    )
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

    logger.info(
        "Activation intent scheduled (legacy is_job_running flag): %s",
        context["is_job_running"],
    )

    # set up response
    resp_user = MessagingResponse()

    # publish to pubsub
    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    logger.info(f"Publishing message to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "msg",
                    "publish_timestamp": time.time(),
                    "event": {
                        "contacts": contacts,
                        "to_number": to_number,
                        "from_number": from_number,
                        "body": body,
                    },
                },
            ).encode("utf-8"),
            thread="inbound",
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            logger.info(f"Message ID: {message_id}")
        logger.info("Message published to Pub/Sub successfully")
    except Exception as e:
        logger.error(f"Error publishing to Pub/Sub: {e}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    logger.info("Returning TwiML response")
    return Response(content=str(resp_user), media_type="text/xml")


# WhatsApp media size limits (bytes)
_WA_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_WA_MEDIA_MAX_BYTES = 16 * 1024 * 1024  # 16 MB


async def _ingest_whatsapp_media(
    form_data,
    assistant_id: str,
    message_sid: str | None,
) -> list[dict]:
    """Download media from Twilio, re-upload to GCS, delete from Twilio.

    Returns a list of attachment dicts compatible with the Unify attachment
    schema: {id, filename, gs_url, content_type, size_bytes}.
    """
    num_media = int(form_data.get("NumMedia", "0") or "0")
    if num_media == 0:
        return []

    creds_json = json.loads(os.getenv("GCP_SA_KEY", "{}"))
    if not creds_json:
        logger.error("GCP_SA_KEY not configured — skipping WhatsApp media ingestion")
        return []

    account_sid = os.getenv("TWILIO_WA_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_WA_AUTH_TOKEN", "")
    twilio_auth = (account_sid, auth_token)

    creds = Credentials.from_service_account_info(creds_json)
    storage_client = storage.Client(credentials=creds)
    bucket = storage_client.bucket(UNIFY_ATTACHMENTS_BUCKET)

    attachments: list[dict] = []

    async with httpx.AsyncClient() as client:
        for i in range(num_media):
            media_url = form_data.get(f"MediaUrl{i}")
            content_type = form_data.get(
                f"MediaContentType{i}",
                "application/octet-stream",
            )
            if not media_url:
                continue

            try:
                resp = await client.get(
                    media_url,
                    auth=twilio_auth,
                    follow_redirects=True,
                    timeout=30.0,
                )
                resp.raise_for_status()
            except Exception:
                logger.exception(f"Failed to download WhatsApp media from {media_url}")
                continue

            file_content = resp.content
            size_bytes = len(file_content)

            max_size = (
                _WA_IMAGE_MAX_BYTES
                if content_type.startswith("image/")
                else _WA_MEDIA_MAX_BYTES
            )
            if size_bytes > max_size:
                logger.warning(
                    f"WhatsApp media exceeds size limit ({size_bytes} > {max_size}), skipping",
                )
                continue

            ext = mimetypes.guess_extension(content_type) or ""
            attachment_id = str(uuid.uuid4())
            filename = f"whatsapp_media_{attachment_id[:8]}{ext}"

            blob_path = f"{assistant_id}/whatsapp/{attachment_id}_{filename}"
            blob = bucket.blob(blob_path)
            await asyncio.to_thread(
                blob.upload_from_string,
                file_content,
                content_type=content_type,
            )

            gs_url = f"gs://{UNIFY_ATTACHMENTS_BUCKET}/{blob_path}"
            logger.info(f"Uploaded WhatsApp media to {gs_url} ({size_bytes} bytes)")

            attachments.append(
                {
                    "id": attachment_id,
                    "filename": filename,
                    "gs_url": gs_url,
                    "content_type": content_type,
                    "size_bytes": size_bytes,
                },
            )

            # Delete media from Twilio to free storage.
            if message_sid:
                media_sid = media_url.rstrip("/").rsplit("/", 1)[-1]
                try:
                    twilio_client = get_twilio_wa_client()
                    await asyncio.to_thread(
                        twilio_client.messages(message_sid).media(media_sid).delete,
                    )
                    logger.info(f"Deleted Twilio media {media_sid}")
                except Exception:
                    logger.exception(f"Failed to delete Twilio media {media_sid}")

    return attachments


@app.post("/twilio/whatsapp", dependencies=[Depends(validate_twilio_wa_signature)])
async def twilio_whatsapp_webhook(request: Request):
    """WhatsApp webhook endpoint - handles incoming Twilio WhatsApp messages."""
    logger.info("twilio_whatsapp_webhook function started")
    form_data = await request.form()

    to_number = form_data.get("To", "") or ""
    from_number = form_data.get("From", "") or ""
    body = form_data.get("Body", "") or ""
    message_sid = form_data.get("MessageSid")
    logger.info(
        f"Received WhatsApp message from {_redact_phone(from_number)} to {_redact_phone(to_number)}",
    )

    pool_number = to_number.replace("whatsapp:", "").strip()
    sender = from_number.replace("whatsapp:", "").strip()

    # Handle VOICE_CALL_REQUEST permission responses before normal routing.
    # When a user responds to a call permission request, Twilio sends a
    # webhook with Body="VOICE_CALL_REQUEST" and ButtonPayload=ACCEPTED|REJECTED.
    if body == "VOICE_CALL_REQUEST":
        button_payload = form_data.get("ButtonPayload", "")
        logger.info(
            f"WhatsApp call permission response from {_redact_phone(sender)}: "
            f"{button_payload}",
        )
        status = "accepted" if button_payload == "ACCEPTED" else "rejected"

        # Forward permission state to Orchestra
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{SETTINGS.orchestra_url}/admin/whatsapp/call-permission",
                    headers={
                        "Authorization": f"Bearer {SETTINGS.orchestra_admin_key}",
                    },
                    json={
                        "pool_number": pool_number,
                        "contact_number": sender,
                        "status": status,
                    },
                    timeout=10.0,
                )
        except Exception:
            logger.exception("Failed to forward call permission to Orchestra")

        # Resolve assistant so we can publish to the right Pub/Sub topic
        resolve_data = await asyncio.to_thread(
            resolve_whatsapp_route,
            pool_number,
            sender,
        )
        if resolve_data and "assistant_id" in resolve_data:
            resolved_id = str(resolve_data["assistant_id"])
            context = await asyncio.to_thread(
                build_webhook_context,
                "whatsapp",
                to_number,
                from_number,
                assistant_id=resolved_id,
                validate_contact=False,
            )
            assistant_id = context["assistant"]["assistant_id"]
            contacts = context["contacts"]

            pubsub_client = get_pubsub_client()
            topic_name = SETTINGS.assistant_topic(assistant_id)
            topic_path = pubsub_client.topic_path(
                SETTINGS.gcp_project_id,
                topic_name,
            )
            try:
                pubsub_client.publish(
                    topic_path,
                    json.dumps(
                        {
                            "thread": "whatsapp",
                            "publish_timestamp": time.time(),
                            "event": {
                                "contacts": contacts,
                                "to_number": to_number,
                                "from_number": from_number,
                                "body": body,
                                "role": resolve_data.get("role", "contact"),
                                "type": "call_permission_response",
                                "payload": button_payload,
                            },
                        },
                    ).encode("utf-8"),
                    thread="inbound",
                )
                logger.info("Call permission response published to Pub/Sub")
            except Exception as e:
                logger.error(f"Error publishing permission response: {e}")

        resp_user = MessagingResponse()
        return Response(content=str(resp_user), media_type="text/xml")

    resolve_data = await asyncio.to_thread(resolve_whatsapp_route, pool_number, sender)

    action = resolve_data.get("action") if resolve_data else None

    if resolve_data is None or action == "auto_reply":
        resp_user = MessagingResponse()
        resp_user.message(
            "This number is no longer active. Please visit "
            "console.unify.ai to view your assistant details.",
        )
        return Response(content=str(resp_user), media_type="text/xml")

    if action == "reject_cold":
        resp_user = MessagingResponse()
        resp_user.message("This number is not accepting new messages.")
        return Response(content=str(resp_user), media_type="text/xml")

    resolved_assistant_id = str(resolve_data["assistant_id"])
    role = resolve_data["role"]

    # Build context using the resolved assistant (skip contact validation —
    # the resolve endpoint already confirmed this sender is valid).
    context = await asyncio.to_thread(
        build_webhook_context,
        "whatsapp",
        to_number,
        from_number,
        assistant_id=resolved_assistant_id,
        validate_contact=False,
    )
    assistant_data = context["assistant"]
    assistant_id = assistant_data["assistant_id"]
    contacts = context["contacts"]

    attachments = await _ingest_whatsapp_media(form_data, assistant_id, message_sid)

    resp_user = MessagingResponse()

    event_data = {
        "contacts": contacts,
        "to_number": to_number,
        "from_number": from_number,
        "body": body,
        "role": role,
    }
    if attachments:
        event_data["attachments"] = attachments

    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    logger.info(f"Publishing message to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "whatsapp",
                    "publish_timestamp": time.time(),
                    "event": event_data,
                },
            ).encode("utf-8"),
            thread="inbound",
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            logger.info(f"Message ID: {message_id}")
        logger.info("Message published to Pub/Sub successfully")
    except Exception as e:
        logger.error(f"Error publishing to Pub/Sub: {e}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    logger.info("Returning TwiML response")
    return Response(content=str(resp_user), media_type="text/xml")


# =============================================================================
# WhatsApp Business Calling Webhook
# =============================================================================


@app.post(
    "/twilio/whatsapp-call",
    dependencies=[Depends(validate_twilio_wa_signature)],
)
async def twilio_whatsapp_call_webhook(request: Request):
    """Inbound WhatsApp Business Calling webhook.

    Triggered by a TwiML Voice Application attached to a WhatsApp sender.
    Bridges the WhatsApp VoIP caller into a Twilio Conference and connects
    a SIP leg to LiveKit so the voice agent can participate.
    """
    logger.info("twilio_whatsapp_call_webhook function started")
    form_data = await request.form()

    to_raw = form_data.get("To", "") or ""
    from_raw = form_data.get("From", "") or ""
    pool_number = to_raw.replace("whatsapp:", "").strip()
    caller_number = from_raw.replace("whatsapp:", "").strip()
    logger.info(
        f"Received WhatsApp call from {_redact_phone(caller_number)} "
        f"to {_redact_phone(pool_number)}",
    )

    # Resolve assistant via the shared WhatsApp pool routing
    resolve_data = await asyncio.to_thread(
        resolve_whatsapp_route,
        pool_number,
        caller_number,
    )
    if resolve_data is None or resolve_data.get("action") in (
        "auto_reply",
        "reject_cold",
    ):
        resp = VoiceResponse()
        resp.say(
            "This number is no longer active. Please visit "
            "console.unify.ai to view your assistant details.",
        )
        resp.hangup()
        return Response(content=str(resp), media_type="text/xml")

    resolved_assistant_id = str(resolve_data["assistant_id"])

    context = await asyncio.to_thread(
        build_webhook_context,
        "whatsapp_call",
        to_raw,
        from_raw,
        assistant_id=resolved_assistant_id,
        validate_contact=False,
    )
    assistant_data = context["assistant"]
    assistant_id = assistant_data["assistant_id"]
    contacts = context["contacts"]

    # Conference + LiveKit room
    date_time = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    conference_name = f"Unity_WA_{pool_number[1:]}_{date_time}"
    room_name = make_room_name(assistant_id, "whatsapp_call")
    sip_uri = make_sip_uri(pool_number)
    logger.info(f"Setting up WhatsApp call conference {conference_name}")
    logger.info(f"LiveKit room: {room_name}")

    await ensure_phone_dispatch_rule(pool_number, room_name)

    # Publish to Pub/Sub
    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    logger.info(f"Publishing WhatsApp call to Pub/Sub at path: {topic_path}")
    try:
        pubsub_message = {
            "thread": "whatsapp_call",
            "publish_timestamp": time.time(),
            "event": {
                "contacts": contacts,
                "conference_name": conference_name,
                "caller_number": caller_number,
                "sip_uri": sip_uri,
                "livekit_room": room_name,
                "assistant_id": assistant_id,
                "action": "start_worker",
                "timestamp": int(time.time() * 1000),
                "call_metadata": {
                    "whatsapp_number": pool_number,
                    "call_type": "inbound",
                    "room_created": True,
                    "bridge_established": True,
                },
            },
        }
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(pubsub_message).encode("utf-8"),
            thread="inbound",
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            logger.info(f"Message ID: {message_id}")
        logger.info("WhatsApp call published to Pub/Sub successfully")
    except Exception as e:
        logger.error(f"Error publishing WhatsApp call to Pub/Sub: {e}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    # Bridge: put the WhatsApp caller in a conference and dial SIP to LiveKit.
    # Both legs must use the WA Twilio account so they share the same
    # conference namespace as the inbound WhatsApp call.
    try:
        resp_user = create_conference_response(conference_name)

        wa_client = get_twilio_wa_client()
        sip_twiml = str(create_conference_response(conference_name))
        call = wa_client.calls.create(
            to=sip_uri,
            from_=pool_number,
            twiml=sip_twiml,
        )
        logger.info(f"SIP leg created for WhatsApp call. Call SID: {call.sid}")
    except Exception as e:
        logger.error(f"Error during WhatsApp call conference setup: {e}")
        return Response(content="Error setting up conference", status_code=500)

    # Recording via LiveKit Egress (fire-and-forget)
    try:
        user_id = assistant_data["user_id"]
        await start_room_egress(room_name, assistant_id, user_id)
    except Exception as e:
        logger.error(
            f"[Egress] Non-fatal: failed to start egress for WhatsApp call: {e}",
        )

    logger.info("Returning TwiML response for WhatsApp call")
    return Response(content=str(resp_user), media_type="text/xml")


@app.post(
    "/twilio/whatsapp-call-status",
    dependencies=[Depends(validate_twilio_wa_signature)],
)
async def twilio_whatsapp_call_status_webhook(request: Request):
    """Status callback for outbound WhatsApp Business Calling.

    Publishes whatsapp_call_answered / whatsapp_call_not_answered events
    to Pub/Sub so Unity can track the call lifecycle.
    """
    form_data = await request.form()
    call_status = form_data.get("CallStatus")
    from_raw = form_data.get("From", "") or ""
    to_raw = form_data.get("To", "") or ""
    pool_number = from_raw.replace("whatsapp:", "").strip()
    user_number = to_raw.replace("whatsapp:", "").strip()
    logger.info(
        f"twilio_whatsapp_call_status_webhook: {call_status} "
        f"from {_redact_phone(pool_number)} to {_redact_phone(user_number)}",
    )

    if call_status not in (
        "in-progress",
        "no-answer",
        "busy",
        "canceled",
        "failed",
    ):
        return Response(status_code=200)

    resolve_data = await asyncio.to_thread(
        resolve_whatsapp_route,
        pool_number,
        user_number,
    )
    if not resolve_data or "assistant_id" not in resolve_data:
        logger.warning("Could not resolve assistant for WhatsApp call status")
        return Response(status_code=200)

    resolved_id = str(resolve_data["assistant_id"])
    context = await asyncio.to_thread(
        build_webhook_context,
        "whatsapp_call",
        from_raw,
        to_raw,
        assistant_id=resolved_id,
        validate_contact=False,
    )
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]

    thread = (
        "whatsapp_call_answered"
        if call_status == "in-progress"
        else "whatsapp_call_not_answered"
    )

    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    logger.info(f"Publishing {thread} to Pub/Sub at path: {topic_path}")
    try:
        pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": thread,
                    "publish_timestamp": time.time(),
                    "event": {
                        "contacts": contacts,
                        "assistant_id": assistant_id,
                        "user_number": user_number,
                        "assistant_number": pool_number,
                        "call_status": call_status,
                        "timestamp": int(time.time() * 1000),
                    },
                },
            ).encode("utf-8"),
            thread="inbound",
        )
        logger.info(f"{thread} published to Pub/Sub successfully")
    except Exception as e:
        logger.error(f"Error publishing WhatsApp call status to Pub/Sub: {e}")

    return Response(status_code=200)


# =============================================================================
# Teams SIP Webhooks
# =============================================================================


@app.post("/teams/call")
async def teams_call_webhook(request: Request):
    """
    Webhook called by Kamailio SBC when a Teams call arrives.
    Publishes to Pub/Sub to trigger agent dispatch, then returns
    so Kamailio can forward the call to LiveKit.

    Authenticated via admin_key in the JSON body (Kamailio's http_client
    cannot send Authorization headers, so the key is passed in the payload).
    """
    logger.info("teams_call_webhook function started")

    try:
        data = await request.json()
    except Exception:
        form_data = await request.form()
        data = dict(form_data)

    import secrets as _secrets

    body_key = data.pop("admin_key", "")
    expected_key = SETTINGS.orchestra_admin_key
    if (
        not expected_key
        or not body_key
        or not _secrets.compare_digest(str(body_key), expected_key)
    ):
        raise HTTPException(status_code=403, detail="Invalid admin key")

    from_uri = data.get("from_uri", "")
    to_uri = data.get("to_uri", "")
    call_id = data.get("call_id", "")
    source_ip = data.get("source_ip", "")

    logger.info(f"Teams call: call_id={call_id}")

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

    logger.info(f"Extracted Teams number: {_redact_phone(teams_number)}")

    if not teams_number:
        logger.error("Could not extract phone number from to_uri")
        return Response(
            content=json.dumps({"error": "Invalid to_uri"}),
            status_code=400,
            media_type="application/json",
        )

    # Look up assistant by the Teams virtual number
    # This uses the same flow as Twilio - the number maps to an assistant
    try:
        context = await asyncio.to_thread(
            build_webhook_context,
            "meet",
            teams_number,
            from_uri,
            validate_contact=False,
        )
        assistant_id = context["assistant"]["assistant_id"]
        contacts = context["contacts"]
    except Exception as e:
        logger.error(f"Could not find assistant for {_redact_phone(teams_number)}: {e}")
        # Return success anyway - let the call proceed, it just won't have an agent
        return Response(
            content=json.dumps({"success": True, "warning": "No assistant found"}),
            status_code=200,
            media_type="application/json",
        )

    room_name = make_room_name(assistant_id, "teams")
    sip_uri = f"sip:{room_name}@{os.getenv('LIVEKIT_SIP_URI')}"  # Teams uses SBC proxy with IP-based trunk

    # print(f"Teams call for assistant {assistant_id}, room: {room_name}")

    # Publish to Pub/Sub (same format as Twilio webhook)
    pubsub_client = get_pubsub_client()
    # topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_name = "test"
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    logger.info(f"Publishing Teams call to Pub/Sub at path: {topic_path}")

    try:
        pubsub_message = {
            "thread": "call",
            "publish_timestamp": time.time(),
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
            thread="inbound",
        )
        # Don't wait for result - fire and forget for speed
        logger.info("Teams call published to Pub/Sub successfully")
    except Exception as e:
        logger.error(f"Error publishing Teams call to Pub/Sub: {e}")
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


class ScheduledPayload(BaseModel):
    test: bool = False


class ScheduledTaskDuePayload(BaseModel):
    """Payload delivered by Cloud Tasks when a scheduled task becomes due."""

    assistant_id: str
    task_id: int
    source_task_log_id: int
    activation_revision: str
    scheduled_for: datetime
    execution_mode: str = "live"
    source_type: str = "scheduled"
    task_label: str = ""
    task_summary: str = ""
    visibility_policy: str = "silent_by_default"
    recurrence_hint: str = "one_off"


# =============================================================================
# Unify Attachment Upload
# =============================================================================

# Bucket for storing unify message attachments (single bucket for all environments)
UNIFY_ATTACHMENTS_BUCKET = os.getenv(
    "ASSISTANT_MESSAGE_ATTACHMENTS_BUCKET_NAME",
    "assistant-message-attachments",
)


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


@app.post("/unify/attachment", dependencies=[Depends(require_admin_key)])
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
    logger.info("unify_attachment_upload function started")

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

        # Generate unique ID for the attachment
        attachment_id = str(uuid.uuid4())

        # Build GCS path: {user_id}/{uuid}_{filename}
        blob_path = f"{assistant_id}/{attachment_id}_{safe_filename}"

        # Get GCP credentials and upload
        creds_json = json.loads(os.getenv("GCP_SA_KEY", "{}"))
        if not creds_json:
            logger.error("GCP_SA_KEY not configured")
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
        logger.info(f"Uploaded attachment to {gs_url}")

        # Generate signed download URL (1 hour expiry for immediate use)
        signed_url = blob.generate_signed_url(
            version="v4",
            expiration=timedelta(hours=1),
            method="GET",
        )

        logger.info(f"Generated signed URL for attachment {attachment_id}")

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
        logger.error(f"Error uploading attachment: {e}", exc_info=True)
        return Response(
            content=json.dumps({"error": f"Failed to upload attachment: {str(e)}"}),
            status_code=500,
            media_type="application/json",
        )


# =============================================================================
# Unify Message
# =============================================================================


@app.post("/unify/message", dependencies=[Depends(require_admin_key)])
async def unify_message_webhook(request: Request):
    """
    Unify message webhook - handles internal message events.

    Accepts an optional 'attachments' array in the request body. Each attachment
    should be an object with: {"id": str, "filename": str, "url": str}
    (as returned by /unify/attachment).
    """
    logger.info("unify_message_webhook function started")

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
        logger.info("Assistant ID is required")
        return Response(status_code=400)

    if contact_id is None:
        logger.info("contact_id is required for unify_message")
        return Response(status_code=400, content="contact_id is required")

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
            logger.info(f"Skipping invalid attachment: {att}")

    attachment_info = (
        f" with {len(validated_attachments)} attachment(s)"
        if validated_attachments
        else ""
    )
    logger.info(
        f"Received unify_message for assistant_id={assistant_id_input}{attachment_info}",
    )

    # shared context
    context = await asyncio.to_thread(
        build_webhook_context,
        channel="unify_message",
        destination="",
        sender="",
        assistant_id=assistant_id_input,
        validate_contact=False,
        ensure_job=True,
    )
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]
    logger.info(
        "Activation intent scheduled (legacy is_job_running flag): %s",
        context["is_job_running"],
    )

    # publish to pubsub
    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    logger.info(f"Publishing unify_message to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "unify_message",
                    "publish_timestamp": time.time(),
                    "event": {
                        "contact_id": contact_id,
                        "contacts": contacts,
                        "assistant_id": assistant_id,
                        "body": body,
                        "attachments": validated_attachments,
                    },
                },
            ).encode("utf-8"),
            thread="inbound",
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            logger.info(f"Message ID: {message_id}")
        logger.info("unify_message message published to Pub/Sub successfully")
    except Exception as e:
        logger.error(f"Error publishing unify_message to Pub/Sub: {e}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    return Response(status_code=200)


# =============================================================================
# API Message
# =============================================================================


@app.post("/api/message", dependencies=[Depends(require_admin_key)])
async def api_message_webhook(request: Request):
    """
    API message webhook — handles programmatic messages sent via Orchestra's
    REST API. Ensures the assistant's Unity job is running before publishing.
    Supports optional file attachments and developer-supplied tags.
    """
    payload = await request.json()
    assistant_id_input = payload.get("assistant_id", "")
    api_message_id = payload.get("api_message_id", "")
    body = payload.get("body", "") or ""
    attachments = payload.get("attachments") or []
    tags = payload.get("tags") or []

    if not assistant_id_input:
        return Response(status_code=400, content="assistant_id is required")
    if not api_message_id:
        return Response(status_code=400, content="api_message_id is required")

    validated_attachments = []
    for att in attachments:
        if (
            isinstance(att, dict)
            and att.get("id")
            and att.get("filename")
            and (att.get("url") or att.get("gs_url"))
        ):
            validated_att = {
                "id": str(att["id"]),
                "filename": str(att["filename"]),
                "url": str(att.get("url", "")),
            }
            if att.get("gs_url"):
                validated_att["gs_url"] = str(att["gs_url"])
            if att.get("content_type"):
                validated_att["content_type"] = str(att["content_type"])
            if att.get("size_bytes") is not None:
                validated_att["size_bytes"] = int(att["size_bytes"])
            validated_attachments.append(validated_att)
        else:
            logger.info(f"Skipping invalid api_message attachment: {att}")

    context = await asyncio.to_thread(
        build_webhook_context,
        channel="api_message",
        destination="",
        sender="",
        assistant_id=assistant_id_input,
        validate_contact=False,
        ensure_job=True,
    )
    assistant_id = context["assistant"]["assistant_id"]

    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    try:
        event_data = {
            "api_message_id": api_message_id,
            "body": body,
            "contact_id": 1,
            "assistant_id": assistant_id,
        }
        if validated_attachments:
            event_data["attachments"] = validated_attachments
        if tags:
            event_data["tags"] = tags
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "api_message",
                    "publish_timestamp": time.time(),
                    "event": event_data,
                },
            ).encode("utf-8"),
            thread="inbound",
        )
        if "test" in assistant_id:
            publish_future.result(timeout=10)
    except Exception as e:
        logger.error(f"Error publishing api_message to Pub/Sub: {e}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    return Response(status_code=200)


@app.post("/unify/meet", dependencies=[Depends(require_admin_key)])
async def unify_meet_webhook(request: Request):
    """Unify meet webhook - handles internal meet events."""
    logger.info("unify_meet_webhook function started")

    # accept JSON or form payloads
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form_data = await request.form()
        payload = dict(form_data)

    room_name = payload.get("room_name", "")
    livekit_agent_name = payload.get("livekit_agent_name", "") or room_name
    if not room_name:
        logger.info("room_name is required")
        return Response(status_code=400)

    assistant_id_input = payload.get("assistant_id", "")
    if not assistant_id_input:
        logger.info("assistant_id is required")
        return Response(status_code=400)

    logger.info(
        f"Received unify_meet for assistant_id={assistant_id_input} room={room_name} livekit_agent_name={livekit_agent_name}",
    )

    # shared context
    context = await asyncio.to_thread(
        build_webhook_context,
        channel="unify_meet",
        destination="",
        sender="",
        assistant_id=assistant_id_input,
        validate_contact=False,
        ensure_job=True,
    )
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]
    logger.info(
        "Activation intent scheduled (legacy is_job_running flag): %s",
        context["is_job_running"],
    )

    # publish to pubsub
    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    logger.info(f"Publishing unify_meet to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "unify_meet",
                    "publish_timestamp": time.time(),
                    "event": {
                        "contacts": contacts,
                        "assistant_id": assistant_id,
                        "livekit_room": room_name,
                        "livekit_agent_name": livekit_agent_name,
                        "timestamp": int(time.time() * 1000),
                    },
                },
            ).encode("utf-8"),
            thread="inbound",
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            logger.info(f"Message ID: {message_id}")
        logger.info("unify_meet message published to Pub/Sub successfully")
    except Exception as e:
        logger.error(f"Error publishing unify_meet to Pub/Sub: {e}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    return Response(status_code=200)


# =============================================================================
# Unity System Webhooks
# =============================================================================


def _build_task_due_reason(payload: ScheduledTaskDuePayload) -> dict:
    """Return the canonical wake reason / system-event payload for due tasks."""

    return {
        "type": "task_due",
        "task_id": payload.task_id,
        "source_task_log_id": payload.source_task_log_id,
        "activation_revision": payload.activation_revision,
        "scheduled_for": payload.scheduled_for.astimezone(timezone.utc).isoformat(),
        "execution_mode": payload.execution_mode,
        "source_type": payload.source_type,
        "task_label": payload.task_label,
        "task_summary": payload.task_summary,
        "visibility_policy": payload.visibility_policy,
        "recurrence_hint": payload.recurrence_hint,
    }


def _task_due_message(payload: ScheduledTaskDuePayload) -> str:
    """Return the human-readable summary attached to a due-task event."""

    scheduled_for = payload.scheduled_for.astimezone(timezone.utc).isoformat()
    if payload.task_label:
        return f"Scheduled task '{payload.task_label}' became due at {scheduled_for}."
    return f"Scheduled task {payload.task_id} became due at {scheduled_for}."


def _publish_unity_system_event(
    *,
    assistant_id: str,
    event_type: str,
    message: str,
    contacts: list[dict] | None = None,
    extra_event_fields: dict | None = None,
) -> None:
    """Publish a Unity system event to the assistant's Pub/Sub topic."""

    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    event_payload = {
        "contacts": contacts or [],
        "assistant_id": assistant_id,
        "event_type": event_type,
        "message": message,
    }
    if extra_event_fields:
        event_payload.update(extra_event_fields)

    publish_future = pubsub_client.publish(
        topic_path,
        json.dumps(
            {
                "thread": "unity_system_event",
                "publish_timestamp": time.time(),
                "event": event_payload,
            },
        ).encode("utf-8"),
        thread="inbound",
    )
    if "test" in str(assistant_id):
        message_id = publish_future.result(timeout=10)
        logger.info(f"Message ID: {message_id}")


@app.post("/unity/system-event", dependencies=[Depends(require_admin_key)])
async def unity_system_event_webhook(request: Request):
    """Unity system event webhook - handles system-level events."""
    logger.info("unity_system_event_webhook function started")

    # accept JSON or form payloads
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form_data = await request.form()
        payload = dict(form_data)

    assistant_id = payload.get("assistant_id", "")
    if not assistant_id:
        logger.info("assistant_id is required")
        return Response(status_code=400)

    event_type = payload.get("event_type", "")
    if not event_type:
        logger.info("event_type is required")
        return Response(status_code=400)

    message = payload.get("message", "")
    if not message:
        logger.info("message is required")
        return Response(status_code=400)

    logger.info(
        f"Received unity_system_event for event_type={event_type}",
    )

    # shared context
    context = await asyncio.to_thread(
        build_webhook_context,
        channel="unity_system_event",
        destination="",
        sender="",
        assistant_id=assistant_id,
        validate_contact=False,
        ensure_job=True,
    )
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]
    logger.info(
        "Activation intent scheduled (legacy is_job_running flag): %s",
        context["is_job_running"],
    )

    try:
        _publish_unity_system_event(
            assistant_id=assistant_id,
            event_type=event_type,
            message=message,
            contacts=contacts,
        )
        logger.info("unity_system_event message published to Pub/Sub successfully")
    except Exception as e:
        logger.error(f"Error publishing unity_system_event to Pub/Sub: {e}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    return Response(status_code=200)


@app.post("/scheduled/tasks/due", dependencies=[Depends(require_admin_key)])
async def scheduled_task_due_webhook(payload: ScheduledTaskDuePayload):
    """Wake or notify an assistant when a scheduled task becomes due."""

    assistant_data = await asyncio.to_thread(
        get_assistant,
        assistant_id=payload.assistant_id,
    )
    if not assistant_data or not assistant_data.get("assistant_id"):
        logger.info(
            "Skipping task_due delivery because assistant %s no longer exists",
            payload.assistant_id,
        )
        return {
            "success": True,
            "status": "skipped",
            "reason": "assistant_not_found",
        }

    assistant_id = assistant_data["assistant_id"]
    wake_reason = _build_task_due_reason(payload)

    try:
        if uses_local_unity_runtime(assistant_data):
            _publish_unity_system_event(
                assistant_id=assistant_id,
                event_type="task_due",
                message=_task_due_message(payload),
                extra_event_fields=wake_reason,
            )
            return {
                "success": True,
                "status": "published_local",
                "assistant_id": assistant_id,
            }

        response = await asyncio.to_thread(
            dispatch_unity_start_intent,
            assistant_data,
            "api_message",
            wake_reasons=[wake_reason],
            timeout_seconds=30,
        )
    except requests.RequestException as exc:
        logger.error(
            "Failed dispatching scheduled task due wake for assistant %s: %s",
            assistant_id,
            exc,
        )
        return Response(
            content=f"Failed to dispatch task due wake: {exc}",
            status_code=500,
        )

    if response is None:
        return Response(
            content="Assistant is missing an API key for task due delivery",
            status_code=500,
        )
    if response.status_code != 200:
        return Response(content=response.text, status_code=response.status_code)

    try:
        start_result = response.json()
    except ValueError as exc:
        logger.error("Invalid /infra/job/start response for task_due: %s", exc)
        return Response(
            content="Invalid /infra/job/start response",
            status_code=500,
        )

    if start_result.get("active_session_already_running"):
        try:
            _publish_unity_system_event(
                assistant_id=assistant_id,
                event_type="task_due",
                message=_task_due_message(payload),
                extra_event_fields=wake_reason,
            )
        except Exception as exc:
            logger.error(
                "Failed publishing task_due system event for assistant %s: %s",
                assistant_id,
                exc,
            )
            return Response(
                content=f"Failed to publish task_due system event: {exc}",
                status_code=500,
            )
        return {
            "success": True,
            "status": "published_to_active_session",
            "assistant_id": assistant_id,
            "activation_id": start_result.get("activation_id"),
        }

    return {
        "success": True,
        "status": "attached_to_startup",
        "assistant_id": assistant_id,
        "activation_id": start_result.get("activation_id"),
    }


@app.post("/unity/pre-hire", dependencies=[Depends(require_admin_key)])
async def unity_pre_hire_webhook(request: Request):
    """Unity pre-hire webhook - logs chat history before hiring."""
    logger.info("unity_pre_hire_webhook function started")

    # accept JSON or form payloads
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = await request.json()
    else:
        form_data = await request.form()
        payload = dict(form_data)

    assistant_id_input = payload.get("assistant_id", "")
    if not assistant_id_input:
        logger.info("Assistant ID is required")
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
        logger.info("Invalid body format; expected list of {role, msg} objects")
        return Response(
            content=json.dumps({"error": "body must be a list of {role, msg}"}),
            status_code=400,
            media_type="application/json",
        )

    logger.info(
        f"Received log_pre_hire_chats for assistant_id={assistant_id_input} with {len(body)} messages",
    )

    # shared context
    context = await asyncio.to_thread(
        build_webhook_context,
        channel="unify_message",
        destination="",
        sender="",
        assistant_id=assistant_id_input,
        validate_contact=False,
        ensure_job=True,
    )
    assistant_id = context["assistant"]["assistant_id"]
    contacts = context["contacts"]
    logger.info(
        "Activation intent scheduled (legacy is_job_running flag): %s",
        context["is_job_running"],
    )

    # publish to pubsub
    pubsub_client = get_pubsub_client()
    topic_name = SETTINGS.assistant_topic(assistant_id)
    topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)
    logger.info(f"Publishing log_pre_hire_chats to Pub/Sub at path: {topic_path}")
    try:
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(
                {
                    "thread": "log_pre_hire_chats",
                    "publish_timestamp": time.time(),
                    "event": {
                        "contacts": contacts,
                        "assistant_id": assistant_id,
                        "body": body,
                    },
                },
            ).encode("utf-8"),
            thread="inbound",
        )
        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            logger.info(f"Message ID: {message_id}")
        logger.info("log_pre_hire_chats message published to Pub/Sub successfully")
    except Exception as e:
        logger.error(f"Error publishing log_pre_hire_chats to Pub/Sub: {e}")
        return Response(content="Error publishing to Pub/Sub", status_code=500)

    return Response(status_code=200)


# =============================================================================
# Assistant Webhooks
# =============================================================================


@app.post("/assistant/wakeup", dependencies=[Depends(require_admin_key)])
async def assistant_wakeup_webhook(request: Request):
    """Accept a wakeup request and dispatch async activation intent.

    A ``200`` here means adapters accepted the wakeup webhook and scheduled the
    best-effort async ``/infra/job/start`` dispatch path. It does not mean
    adapters observed a comms 200/202, that an AssistantSession exists, or that
    the runtime is ready.
    """
    logger.info("assistant_wakeup_webhook function started")
    form_data = await request.form()
    assistant_id = form_data.get("assistant_id")
    logger.info(f"Assistant {assistant_id} woke up")

    # Build shared webhook context and, when needed, schedule best-effort async
    # dispatch of activation intent to comms. Runtime convergence remains
    # asynchronous after this returns.
    await asyncio.to_thread(
        build_webhook_context,
        channel="wakeup",
        destination="",
        sender="",
        assistant_id=assistant_id,
        validate_contact=False,
        ensure_job=True,
    )

    return Response(status_code=200)


@app.post("/assistant/update", dependencies=[Depends(require_admin_key)])
async def assistant_update_webhook(request: Request):
    """
    Publish an assistant update and dispatch activation intent if needed.

    The returned ``200`` only confirms adapters accepted the update request and
    scheduled the best-effort async startup dispatch path. It does not mean
    adapters observed a comms 200/202, that an AssistantSession exists, or that
    runtime is ready.
    """
    logger.info("assistant_update_webhook function started")

    try:
        form_data = await request.form()
        assistant_id = form_data.get("assistant_id")
        logger.info(f"Received assistant_id: {assistant_id}")

        # Use build_webhook_context to handle job startup if needed
        context = await asyncio.to_thread(
            build_webhook_context,
            channel="assistant_update",
            destination="",
            sender="",
            assistant_id=assistant_id,
            validate_contact=False,
            ensure_job=True,
        )
        assistant_data = context["assistant"]
        logger.info(
            "Activation dispatch state (legacy flags): is_job_running=%s, job_started=%s",
            context["is_job_running"],
            context["job_started"],
        )

        # Publish the update after scheduling activation intent. Runtime
        # readiness remains asynchronous downstream.
        pubsub_client = get_pubsub_client()
        topic_name = SETTINGS.assistant_topic(assistant_id)
        topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)

        # Prepare message in the same format as startup event
        message_data = {
            "thread": "assistant_update",
            "publish_timestamp": time.time(),
            "event": assistant_data,
        }

        logger.info(f"Publishing assistant update to Pub/Sub at path: {topic_path}")
        publish_future = pubsub_client.publish(
            topic_path,
            json.dumps(message_data).encode("utf-8"),
            thread="inbound",
        )

        if "test" in assistant_id:
            message_id = publish_future.result(timeout=10)
            logger.info(f"Message ID: {message_id}")

        logger.info("Assistant update published to Pub/Sub successfully")

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
        logger.error(f"Error in assistant_update_webhook: {e}", exc_info=True)
        return Response(
            content=json.dumps({"error": str(e)}),
            status_code=500,
            media_type="application/json",
        )


# =============================================================================
# Email Webhooks
# =============================================================================


@app.post("/email/gmail")
def gmail_notification_processor(envelope: dict = Body(...)):
    """
    Cloud Run endpoint that processes Gmail notifications via Pub/Sub push.
    Receives push messages from Pub/Sub subscription.
    """
    try:
        # Parse the Pub/Sub push message envelope
        if not envelope or "message" not in envelope:
            logger.info("Bad Request: no Pub/Sub message")
            return Response(content="Bad Request: no Pub/Sub message", status_code=400)

        pubsub_message = envelope.get("message", {})
        data = base64.b64decode(pubsub_message.get("data", "")).decode("utf-8")
        notification = json.loads(data)
        logger.info("Received Gmail notification")

        # extract Gmail notification details (mailbox address being watched)
        assistant_email_address = notification["emailAddress"]
        history_id = notification["historyId"]

        # Build Gmail API client.  BYOD accounts have a GOOGLE_ACCESS_TOKEN
        # secret; platform-managed accounts use service-account delegation.
        assistant_data_prefetch = get_assistant(email_address=assistant_email_address)
        google_token = (assistant_data_prefetch.get("secrets") or {}).get(
            "GOOGLE_ACCESS_TOKEN",
        )
        if google_token:
            from google.oauth2.credentials import (
                Credentials as OAuthCredentials,
            )

            gmail_creds = OAuthCredentials(token=google_token)
        else:
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
        logger.info(
            f"assistant_email_address: {_redact_email(assistant_email_address)}, history_id: {history_id}",
        )
        thread_id, email_id, last_message, gmail_message_id = get_thread_id(
            assistant_email_address,
            history_id,
            gmail_service,
        )
        logger.info(
            f"thread_id: {thread_id}, email_id: {email_id}",
        )
        if not thread_id:
            logger.info(
                f"No new conversations found for user {_redact_email(assistant_email_address)}",
            )
            return Response(content="No new conversations", status_code=200)

        from_email = last_message["sender"].split("<")[1].split(">")[0]
        logger.info(f"from_email: {_redact_email(from_email)}")

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

        logger.info(
            "Activation intent scheduled (legacy is_job_running flag): %s",
            context["is_job_running"],
        )

        logger.info(
            f"Successfully processed conversation for user {_redact_email(assistant_email_address)}",
        )
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
        logger.error(error_message, exc_info=True)
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
        expected_secret = os.environ.get("OUTLOOK_WEBHOOK_SECRET", "")
        if not expected_secret:
            logger.error("OUTLOOK_WEBHOOK_SECRET not configured")
            return Response(status_code=500)

        # Validate and extract email from clientState
        if "::" in client_state:
            secret_part, assistant_email_address = client_state.split("::", 1)
            if secret_part != expected_secret:
                logger.error("Invalid clientState secret")
                return Response(status_code=200)
        else:
            # Legacy format - just the secret, no email encoded
            logger.info("Legacy clientState format (no email encoded)")
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
            logger.error(f"Could not parse message ID from resource path: {e}")
            return Response(status_code=200)

        logger.info(
            f"assistant_email_address: {_redact_email(assistant_email_address)}, email_id: {email_id}",
        )

        # Get assistant data, secrets, and contacts in one call
        context = await asyncio.to_thread(
            build_webhook_context,
            channel="email",
            destination=assistant_email_address,
            sender="",  # Unknown until we fetch the message
            validate_contact=False,  # We'll validate after fetching message
            ensure_job=False,  # We'll start job after validation
        )
        assistant_data = context["assistant"]
        if not assistant_data or not assistant_data.get("assistant_id"):
            logger.info(
                f"Assistant not found for {_redact_email(assistant_email_address)}",
            )
            return Response(status_code=200)

        assistant_id = assistant_data["assistant_id"]
        user_id = assistant_data["user_id"]
        api_key = assistant_data["api_key"]

        # Build Graph client: per-user OAuth token if available, else admin app credentials
        secrets = assistant_data.get("secrets", {})
        try:
            graph_client, has_user_token = get_outlook_graph_client(secrets)
        except RuntimeError:
            logger.info(
                f"No Microsoft credentials available for {_redact_email(assistant_email_address)}",
            )
            return Response(status_code=200)

        # Fetch message details to get the actual sender
        conversation_id, email_id, last_message = await get_outlook_thread_id(
            email_id,
            graph_client,
            user_email=None if has_user_token else assistant_email_address,
        )
        logger.info(
            f"conversation_id: {conversation_id}, email_id: {email_id}",
        )

        if not conversation_id:
            logger.info(
                f"No new conversations found for user {_redact_email(assistant_email_address)}",
            )
            return Response(status_code=200)

        from_email = last_message["sender"]
        logger.info(f"from_email: {_redact_email(from_email)}")

        def _process_outlook():
            # Validate contact now that we have the sender
            contacts, is_valid, _matched = check_valid_contact(
                email_address=from_email,
                medium="email",
                assistant_context=f"{user_id}/{assistant_id}",
                api_key=api_key,
                user_number=assistant_data.get("user_number", ""),
                user_whatsapp_number=assistant_data.get("user_whatsapp_number", ""),
                user_email=assistant_data.get("user_email", ""),
                assistant_data=assistant_data,
            )

            if not is_valid:
                error_message = (
                    "This email address is no longer active. Please visit "
                    "console.unify.ai to view your assistant details."
                )
                return Response(content=error_message, status_code=500)

            # Local assistants publish to Pub/Sub but keep runtime local.
            if uses_local_unity_runtime(assistant_data):
                logger.info("Skipped remote job start for local email assistant")
            else:
                start_unity_job(assistant_data, "email")
                logger.info("Job start requested for email handler")

            logger.info(
                f"Successfully processed conversation for user {_redact_email(assistant_email_address)}",
            )
            publish_outlook_thread_id(
                assistant_id,
                user_id,
                conversation_id,
                email_id,
                last_message,
                contacts,
            )
            return Response(content="OK", status_code=200)

        return await asyncio.to_thread(_process_outlook)

    except Exception as e:
        error_message = f"Error processing notification: {str(e)}"
        logger.error(error_message, exc_info=True)
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
        logger.info(f"Received Teams notification: {notification.get('resource', '')}")

        # Validate client state and extract assistant email
        client_state = notification.get("clientState", "")
        expected_secret = os.environ.get("TEAMS_WEBHOOK_SECRET", "")
        if not expected_secret:
            logger.error("TEAMS_WEBHOOK_SECRET not configured")
            return Response(status_code=500)
        parts = client_state.split("::")
        if len(parts) < 2 or parts[0] != expected_secret:
            logger.error("Invalid clientState format")
            return Response(status_code=200)

        assistant_email = parts[1]
        resource = notification.get("resource", "")
        resource_data = notification.get("resourceData", {})

        # Determine message type from resource path.  Per-channel subs
        # emit ``teams('{id}')/channels('{id}')/messages('{id}')``.
        # Chat subs emit ``chats('{id}')/messages('{id}')`` (or under
        # ``users('{id}')/chats/...`` for per-user subs).  We still
        # accept the legacy ``joinedTeams('{id}')`` envelope so any
        # in-flight subs created by the old resource path drain
        # cleanly until they expire.
        resource_lower = resource.lower()
        is_channel_message = (
            "channels(" in resource_lower or "/channels/" in resource_lower
        ) and ("teams(" in resource_lower or "/teams/" in resource_lower)
        is_reply = "replies(" in resource_lower or "/replies/" in resource_lower
        msg_type = "channel" if is_channel_message else "chat"

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
            # Prefer ``joinedTeams`` when present so we don't accidentally
            # extract the literal ``joinedTeams('{id}')`` segment when
            # asking for ``teams``.  ``joinedTeams`` carries the same
            # team id semantically.
            team_id = parse_teams_resource_id(
                resource, "joinedTeams"
            ) or parse_teams_resource_id(resource, "teams")
            channel_id = parse_teams_resource_id(resource, "channels")
            chat_id = None
            if not team_id or not channel_id or not message_id:
                logger.info(
                    f"Missing IDs for channel message: team={team_id}, channel={channel_id}, message={message_id}",
                )
                return Response(status_code=200)
            logger.info(
                f"assistant_email: {_redact_email(assistant_email)}, team_id: {team_id}, channel_id: {channel_id}, message_id: {message_id}",
            )
        else:
            chat_id = resource_data.get("chatId") or parse_teams_resource_id(
                resource,
                "chats",
            )
            team_id = channel_id = None
            if not chat_id or not message_id:
                logger.info(
                    f"Missing IDs for chat message: chat={chat_id}, message={message_id}",
                )
                return Response(status_code=200)
            logger.info(
                f"assistant_email: {_redact_email(assistant_email)}, chat_id: {chat_id}, message_id: {message_id}",
            )

        # Get assistant data
        context = await asyncio.to_thread(
            build_webhook_context,
            channel="teams",
            destination=assistant_email,
            sender="",
            validate_contact=False,
            ensure_job=False,
        )
        assistant_data = context["assistant"]
        if not assistant_data or not assistant_data.get("assistant_id"):
            logger.info(f"Assistant not found for {_redact_email(assistant_email)}")
            return Response(status_code=200)

        assistant_id = assistant_data["assistant_id"]
        user_id = assistant_data["user_id"]
        api_key = assistant_data["api_key"]

        # Pick Graph credentials mode.  BYOD assistants carry a per-user
        # OAuth token and use /me/* paths.  Us-provisioned assistants have
        # no stored token; we mint an app-only bearer against the admin
        # tenant and address the mailbox explicitly via /users/{email}/*.
        user_access_token = assistant_data.get("secrets", {}).get(
            "MICROSOFT_ACCESS_TOKEN",
        )
        is_byod = bool(user_access_token)
        if is_byod:
            bearer = user_access_token
            chat_path_prefix = "/v1.0/me"
        else:
            try:
                bearer = get_admin_graph_bearer_token()
            except Exception as e:
                logger.error(
                    f"Cannot acquire admin bearer for {_redact_email(assistant_email)}: {e}",
                )
                return Response(status_code=200)
            chat_path_prefix = f"/v1.0/users/{quote(assistant_email, safe='@')}"

        # Fetch message from Graph API
        async def graph_get(url: str) -> tuple[dict | None, int]:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {bearer}"},
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
                logger.error(f"Failed to fetch {msg_type} messages (status={status})")
                return Response(status_code=200)

            # Find matching message in list
            for msg in data.get("value", []):
                if msg.get("id") == message_id:
                    message_data = msg
                    break
            if not message_data:
                logger.info(f"Message {message_id} not found in list")
                return Response(status_code=200)
        else:
            url = (
                f"https://graph.microsoft.com{chat_path_prefix}"
                f"/chats/{chat_id}/messages/{message_id}"
            )
            message_data, status = await graph_get(url)
            if not message_data:
                logger.error(f"Failed to fetch chat message (status={status})")
                return Response(status_code=200)

            chat_metadata, _ = await graph_get(
                f"https://graph.microsoft.com{chat_path_prefix}"
                f"/chats/{chat_id}?$select=chatType,topic",
            )
            chat_type = chat_metadata.get("chatType") if chat_metadata else None
            chat_topic = chat_metadata.get("topic") if chat_metadata else None

        # Extract sender info.  Graph returns ``"from": null`` for system /
        # control-plane events (channelAdded, memberJoined, teamJoined,
        # etc.) — the bare ``.get("from", {})`` idiom returns ``None``
        # when the key *exists* but is null, so coalesce explicitly.
        from_field = message_data.get("from") or {}
        sender_user = from_field.get("user") or {}
        sender_name = sender_user.get("displayName") or "Unknown"
        sender_id = sender_user.get("id")
        sender_email = sender_user.get("email") or sender_user.get("userPrincipalName")
        message_type = message_data.get("messageType") or "message"

        # System / event messages have no human sender.  Use them as a
        # signal to re-enumerate channels (new channel / new team / we
        # were just added as a member), then drop.  The scheduled
        # ``/scheduled/teams-watches`` tick is the safety net; this
        # just makes coverage near-real-time.
        if not sender_id and not sender_email:
            event_type = message_data.get("eventDetail", {}).get("@odata.type") or ""
            topology_markers = (
                "channelAdded",
                "channelDeleted",
                "teamJoined",
                "memberAdded",
                "memberJoined",
                "teamRenamed",
                "channelRenamed",
            )
            if is_channel_message and any(m in event_type for m in topology_markers):
                # Fire-and-forget; guarded in-process to avoid a burst of
                # redundant rebuilds when several topology events arrive
                # back-to-back for the same assistant.
                asyncio.create_task(
                    _maybe_rebootstrap_teams_watch(assistant_email),
                )
            # Any membership / topology change invalidates the cached
            # roster for this conversation, regardless of whether a
            # /teams/watch rebuild is warranted.  Chats receive
            # memberAdded / memberLeft events on the same resource path.
            chat_topology_markers = ("memberAdded", "memberLeft", "membersAdded")
            if any(m in event_type for m in topology_markers) or any(
                m in event_type for m in chat_topology_markers
            ):
                _invalidate_teams_roster(
                    chat_id=chat_id,
                    team_id=team_id,
                    channel_id=channel_id,
                )
            logger.info(
                f"Skipping non-message channel event "
                f"(messageType={message_type}, event_type={event_type!r}, "
                f"team={team_id}, channel={channel_id}, msg={message_id})",
            )
            return Response(status_code=200)

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
                # Federated / consumer senders aren't resolvable via
                # ``/users/{id}`` (their id is a cross-tenant proxy, not a
                # tenant user GUID).  Synthesising ``{id}@teams`` keeps
                # the downstream contact pipeline flowing but the contact
                # will look like an unknown external — worth surfacing so
                # operators can correlate "unknown sender" reports.
                logger.warning(
                    f"sender resolve fell through to synthetic placeholder "
                    f"for sender_id={sender_id} (federated / consumer account?)",
                )
                sender_email = f"{sender_id}@teams"

        logger.info(
            f"from_email: {_redact_email(sender_email) if sender_email else 'None'}, sender_name: {sender_name}",
        )

        # Skip self-messages.  The assistant's Teams identity is the
        # same mailbox we're monitoring; any outbound message the
        # runtime itself posts would otherwise loop back here.  Guard
        # *before* resolving contacts — resolution may otherwise match
        # the assistant to its own contact_id=0 default row and mis-
        # attribute the message as inbound-from-boss.
        if sender_email and sender_email.lower() == assistant_email.lower():
            logger.info(
                f"Skipping self-message from {_redact_email(sender_email)}",
            )
            return Response(status_code=200)

        def _validate_and_start():
            contacts, is_valid, matched = check_valid_contact(
                email_address=sender_email,
                medium="teams",
                assistant_context=f"{user_id}/{assistant_id}",
                api_key=api_key,
                user_number=assistant_data.get("user_number", ""),
                user_whatsapp_number=assistant_data.get("user_whatsapp_number", ""),
                user_email=assistant_data.get("user_email", ""),
                assistant_data=assistant_data,
                sender_name=sender_name,
            )
            if not is_valid:
                logger.info(f"Invalid contact: {_redact_email(sender_email)}")
                return None, False, None

            if uses_local_unity_runtime(assistant_data):
                logger.info("Skipped remote job start for local teams assistant")
            else:
                start_unity_job(assistant_data, "teams")
                logger.info("Job start requested for teams handler")
            return contacts, True, matched

        contacts, valid, matched_contact = await asyncio.to_thread(_validate_and_start)
        if not valid:
            return Response(status_code=200)

        # Second-tier self-guard: if the resolver pinned the assistant's
        # own default contact (``contact_id == 0``), we are looking at
        # an outbound/loopback message.  Drop it rather than publish.
        if matched_contact and matched_contact.get("contact_id") == 0:
            logger.info(
                f"Skipping message that resolved to assistant's own contact "
                f"(sender_email={_redact_email(sender_email)})",
            )
            return Response(status_code=200)

        # Cache the (sender_id → contact) mapping so subsequent messages
        # from this federated/external sender match in O(1) without
        # re-running the brittle name-fallback path.  Best-effort: a
        # failure here does not break the current message's delivery.
        if sender_id and sender_email and sender_email.endswith("@teams"):
            await _register_teams_user_id_contact(
                assistant_id=str(assistant_id),
                api_key=api_key,
                sender_id=sender_id,
                sender_name=sender_name,
                synthetic_email=sender_email,
            )

        # Fetch the conversation roster and pre-resolve as many members
        # as possible against the contacts we already loaded for the
        # sender path.  Unity finishes unresolved entries via its
        # canonical unknown-contact creation flow; we deliberately don't
        # mint contacts here so ``ContactManager`` stays the sole writer.
        #
        # Chats and standard channels yield a full roster.  Private /
        # shared channels fall back to "sender + @mentions" because
        # their membership endpoint requires ``ChannelMember.Read.All``
        # which we intentionally don't request.
        participants: list[dict] = []
        participants_incomplete = False
        participants_reason = "ok"
        try:
            roster, participants_incomplete, participants_reason = (
                await _fetch_teams_roster(
                    bearer=bearer,
                    chat_path_prefix=chat_path_prefix,
                    chat_id=chat_id,
                    team_id=team_id,
                    channel_id=channel_id,
                )
            )
            # Layer in signal that does *not* depend on the roster:
            # the sender and anyone the sender @mentioned.  For private
            # and shared channels this is the only participant data we
            # can produce; for chats / standard channels it's additive
            # (usually already in the roster, deduped below).
            augmented = list(roster)
            if sender_id or sender_email:
                augmented.append(
                    {
                        "aad_user_id": sender_id,
                        "email": (
                            sender_email
                            if sender_email and not sender_email.endswith("@teams")
                            else None
                        ),
                        "display_name": sender_name or "",
                        "tenant_id": None,
                    },
                )
            augmented.extend(
                await _extract_message_mentions(
                    message_data=message_data,
                    bearer=bearer,
                ),
            )
            participants = _resolve_roster_participants(
                roster=augmented,
                contacts=contacts,
                assistant_email=assistant_email,
            )
            logger.info(
                f"teams roster: {len(participants)} participants "
                f"({sum(1 for p in participants if p.get('contact_id') is not None)} "
                f"resolved) incomplete={participants_incomplete} "
                f"reason={participants_reason}",
            )
        except Exception as e:
            # Roster is best-effort: sender-path delivery must not
            # regress when /members fails for any reason (scope missing,
            # Graph hiccup, federated tenant).  Publish the event
            # without participants and let downstream fall back to
            # legacy receiver_ids = [0] behaviour.
            logger.info(f"teams roster fetch errored (non-fatal): {e}")
            participants = []
            participants_incomplete = True
            participants_reason = "graph_error"

        # Build event payload
        message_content = message_data.get("body", {}).get("content", "")
        message_content_type = message_data.get("body", {}).get("contentType", "text")
        subject = message_data.get("subject", "")

        raw_attachments = message_data.get("attachments") or []
        attachments = [
            {
                "id": att.get("id", ""),
                "name": att.get("name", ""),
                "content_type": att.get("contentType", ""),
                "content_url": att.get("contentUrl", ""),
            }
            for att in raw_attachments
            if att.get("contentType") == "reference" and att.get("contentUrl")
        ]

        # Resolve the sender asymmetry between comms (name-fallback
        # tolerant) and unity.comms_manager (email-exact).  If we
        # matched a real contact but the only email we had was our
        # synthetic ``{id}@teams`` placeholder, rewrite ``sender`` to
        # the contact's real email so downstream email-keyed lookups
        # hit.  We also pass ``resolved_contact_id`` so unity can
        # skip the re-resolution entirely.
        resolved_contact_id = (
            matched_contact.get("contact_id") if matched_contact else None
        )
        publish_sender = sender_email
        if (
            matched_contact
            and sender_email
            and sender_email.endswith("@teams")
            and matched_contact.get("email_address")
        ):
            publish_sender = matched_contact["email_address"]
            logger.info(
                f"Rewriting synthetic sender {sender_email!r} -> "
                f"{_redact_email(publish_sender)} "
                f"(contact_id={resolved_contact_id})",
            )

        event_data = {
            "contacts": contacts,
            "message_id": message_id,
            "sender": publish_sender,
            "sender_name": sender_name,
            "sender_id": sender_id,
            "resolved_contact_id": resolved_contact_id,
            "body": message_content,
            "content_type": message_content_type,
            "created_at": message_data.get("createdDateTime"),
            "assistant_email": assistant_email,
            "is_channel_message": is_channel_message,
            "timestamp": int(time.time() * 1000),
            "attachments": attachments,
            "participants": participants,
            "participants_incomplete": participants_incomplete,
            "participants_reason": participants_reason,
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
            event_data.update(
                {
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                    "chat_topic": chat_topic,
                    "action": "new_message",
                }
            )

        # Publish to Pub/Sub
        pubsub_client = get_pubsub_client()
        topic_name = SETTINGS.assistant_topic(assistant_id)
        topic_path = pubsub_client.topic_path(SETTINGS.gcp_project_id, topic_name)

        pubsub_message = {
            "thread": "teams_channel" if is_channel_message else "teams_chat",
            "publish_timestamp": time.time(),
            "event": event_data,
        }
        logger.info("Publishing Teams event to Pub/Sub")

        try:
            publish_future = pubsub_client.publish(
                topic_path,
                json.dumps(pubsub_message).encode("utf-8"),
                thread="inbound",
            )
            publish_future.result(timeout=5)
        except Exception as e:
            logger.error(f"Pub/Sub publish error: {e}")
            return Response(content=str(e), status_code=500)

        logger.info(
            f"Successfully processed Teams {msg_type} message for {_redact_email(assistant_email)}",
        )
        return Response(content="OK", status_code=200)

    except Exception as e:
        error_message = f"Error processing Teams notification: {str(e)}"
        logger.error(error_message, exc_info=True)
        return Response(content=error_message, status_code=500)


# In-memory dedupe for topology-driven /teams/watch rebuilds.  When a
# user joins several new channels at once, Graph fires a burst of
# system-event messages; we only need one rebuild per assistant.
_TEAMS_REBOOTSTRAP_DEDUPE_WINDOW_S = 30.0
_teams_rebootstrap_last: dict[str, float] = {}
_teams_rebootstrap_lock = asyncio.Lock()


async def _maybe_rebootstrap_teams_watch(assistant_email: str) -> None:
    """Fire-and-forget rebuild of an assistant's Teams subscriptions.

    Called from ``teams_notification_processor`` when we see a channel
    topology system event (member / channel / team add).  De-duplicates
    within a short window to absorb event bursts.  The 30-min
    ``/scheduled/teams-watches`` tick remains the safety net; this path
    just shortens the time-to-coverage for newly-added channels from
    up to 30 minutes down to seconds.
    """
    if not assistant_email:
        return
    now = time.monotonic()
    key = assistant_email.lower()
    async with _teams_rebootstrap_lock:
        last = _teams_rebootstrap_last.get(key, 0.0)
        if now - last < _TEAMS_REBOOTSTRAP_DEDUPE_WINDOW_S:
            return
        _teams_rebootstrap_last[key] = now

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{SETTINGS.comms_url}/teams/watch",
                json={"primary_email": assistant_email},
                headers={
                    "Authorization": f"Bearer {SETTINGS.orchestra_admin_key}",
                },
                timeout=5.0,
            )
        logger.info(
            f"topology-event: /teams/watch rebuild for "
            f"{_redact_email(assistant_email)} returned {resp.status_code}",
        )
    except Exception as e:
        logger.info(
            f"topology-event: /teams/watch rebuild for "
            f"{_redact_email(assistant_email)} failed: {e}",
        )


# Short-TTL cache for Teams chat / channel rosters.  Graph charges a
# round trip per ``/members`` call; hot chats produce bursts of messages
# that would otherwise refetch the same roster dozens of times.  Entries
# are invalidated on topology system events (memberAdded / memberLeft /
# channelAdded / teamJoined) inside ``teams_notification_processor`` so
# membership changes propagate immediately without waiting for TTL.
_TEAMS_ROSTER_TTL_S = 600.0
_teams_roster_cache: dict[str, tuple[float, list[dict], bool, str]] = {}
_teams_roster_lock = asyncio.Lock()

# Channel ``membershipType`` rarely changes after creation (a standard
# channel cannot become private or vice-versa without recreating), so a
# long TTL is safe.  Covered by the already-granted ``Channel.ReadBasic.All``
# scope — no extra consent required.
_TEAMS_MEMBERSHIP_TYPE_TTL_S = 86_400.0
_teams_membership_type_cache: dict[str, tuple[float, str | None]] = {}
_teams_membership_type_lock = asyncio.Lock()


def _roster_cache_key(
    *,
    chat_id: str | None,
    team_id: str | None,
    channel_id: str | None,
) -> str:
    if chat_id:
        return f"chat::{chat_id}"
    return f"channel::{team_id}::{channel_id}"


def _invalidate_teams_roster(
    *,
    chat_id: str | None,
    team_id: str | None,
    channel_id: str | None,
) -> None:
    key = _roster_cache_key(chat_id=chat_id, team_id=team_id, channel_id=channel_id)
    _teams_roster_cache.pop(key, None)


async def _graph_get_json(
    url: str,
    bearer: str,
    *,
    timeout: float = 15.0,
) -> tuple[dict | None, int]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=timeout,
        )
    return (resp.json() if resp.status_code == 200 else None), resp.status_code


async def _get_channel_membership_type(
    *,
    bearer: str,
    team_id: str,
    channel_id: str,
) -> str | None:
    """Return ``"standard" | "private" | "shared" | None`` for a channel.

    Uses ``GET /teams/{team-id}/channels/{channel-id}?$select=membershipType``
    which requires only ``Channel.ReadBasic.All`` (already granted) and
    is long-cached because membership type is immutable after channel
    creation.  Returns ``None`` when the lookup fails so the caller can
    degrade conservatively (treat unknown as private — no roster
    available).
    """
    key = f"{team_id}::{channel_id}"
    now = time.monotonic()
    async with _teams_membership_type_lock:
        cached = _teams_membership_type_cache.get(key)
        if cached and now - cached[0] < _TEAMS_MEMBERSHIP_TYPE_TTL_S:
            return cached[1]

    encoded_channel_id = quote(channel_id, safe="")
    url = (
        f"https://graph.microsoft.com/v1.0/teams/{team_id}"
        f"/channels/{encoded_channel_id}?$select=membershipType"
    )
    data, status = await _graph_get_json(url, bearer)
    membership_type = (data or {}).get("membershipType") if data else None
    if not membership_type:
        logger.info(
            f"channel membershipType fetch failed: team={team_id} "
            f"channel={channel_id} status={status}",
        )

    async with _teams_membership_type_lock:
        _teams_membership_type_cache[key] = (now, membership_type)
    return membership_type


async def _fetch_teams_roster(
    *,
    bearer: str,
    chat_path_prefix: str,
    chat_id: str | None,
    team_id: str | None,
    channel_id: str | None,
) -> tuple[list[dict], bool, str]:
    """Fetch the conversation roster for a chat or channel.

    Returns ``(members, incomplete, reason)`` where *members* is a list
    of ``{"aad_user_id", "email", "display_name", "tenant_id"}`` dicts,
    *incomplete* is ``True`` when we couldn't fully enumerate the
    roster, and *reason* is one of:

    * ``"ok"`` — roster fetched successfully.
    * ``"graph_error"`` — Graph call failed or partial; we retry on the
      next notification via cache expiry.
    * ``"private_channel"`` / ``"shared_channel"`` — channel's roster
      endpoint requires ``ChannelMember.Read.All`` which is not in our
      scope bundle; callers must layer in sender + message mentions as
      a best-effort participant signal.
    * ``"unknown_channel_type"`` — couldn't resolve ``membershipType``;
      treated conservatively as private (no roster call attempted).

    Endpoints used:

    * Chats (1:1, group, meeting): ``GET {prefix}/chats/{id}/members``
      — covered by ``Chat.Read``.
    * Standard channels: ``GET /teams/{team-id}/members`` — covered by
      ``TeamMember.Read.All``; members of a standard channel equal
      members of its parent team.
    * Private / shared channels: the channel-scoped roster endpoint
      requires ``ChannelMember.Read.All`` which we intentionally don't
      request.  We skip the fetch and let the caller reconstruct a
      lower-bound participant set from the message's sender and
      ``mentions[]`` field.

    Roster results are cached per chat / (team, channel) for up to
    :data:`_TEAMS_ROSTER_TTL_S` seconds.
    """
    key = _roster_cache_key(chat_id=chat_id, team_id=team_id, channel_id=channel_id)
    now = time.monotonic()
    async with _teams_roster_lock:
        cached = _teams_roster_cache.get(key)
        if cached and now - cached[0] < _TEAMS_ROSTER_TTL_S:
            return cached[1], cached[2], cached[3]

    if chat_id:
        encoded_chat_id = quote(chat_id, safe="")
        url = (
            f"https://graph.microsoft.com{chat_path_prefix}"
            f"/chats/{encoded_chat_id}/members"
        )
    else:
        # Channel path: branch on ``membershipType`` because only
        # standard channels have a team-inherited roster reachable
        # without ``ChannelMember.Read.All``.
        if not team_id or not channel_id:
            return [], True, "graph_error"
        membership_type = await _get_channel_membership_type(
            bearer=bearer,
            team_id=team_id,
            channel_id=channel_id,
        )
        if membership_type == "private":
            result = ([], True, "private_channel")
            async with _teams_roster_lock:
                _teams_roster_cache[key] = (now, *result)
            return result
        if membership_type == "shared":
            result = ([], True, "shared_channel")
            async with _teams_roster_lock:
                _teams_roster_cache[key] = (now, *result)
            return result
        if membership_type != "standard":
            # None / unrecognised value — be conservative and treat as
            # roster-unavailable rather than hitting an endpoint that
            # might 403 and poison the cache with graph_error.
            result = ([], True, "unknown_channel_type")
            async with _teams_roster_lock:
                _teams_roster_cache[key] = (now, *result)
            return result
        url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/members"

    members: list[dict] = []
    incomplete = False
    next_url: str | None = url
    while next_url:
        data, status = await _graph_get_json(next_url, bearer)
        if data is None:
            logger.info(
                f"teams roster fetch failed: url={next_url} status={status}",
            )
            incomplete = True
            break
        for m in data.get("value", []) or []:
            aad_id = m.get("userId") or (m.get("user") or {}).get("id")
            email = m.get("email") or None
            display_name = m.get("displayName") or ""
            tenant_id = m.get("tenantId")
            if not aad_id and not email:
                continue
            members.append(
                {
                    "aad_user_id": aad_id,
                    "email": email,
                    "display_name": display_name,
                    "tenant_id": tenant_id,
                },
            )
        next_url = data.get("@odata.nextLink")

    reason = "graph_error" if incomplete else "ok"
    async with _teams_roster_lock:
        _teams_roster_cache[key] = (now, members, incomplete, reason)
    return members, incomplete, reason


async def _extract_message_mentions(
    *,
    message_data: dict,
    bearer: str,
) -> list[dict]:
    """Turn the ChatMessage ``mentions[]`` array into roster-shaped dicts.

    ``ChatMessage.mentions[]`` is returned alongside the message body
    with zero extra Graph calls and zero extra scopes — it's covered by
    the same ``ChannelMessage.Read.All`` / ``ChatMessage.Read`` that
    already authorises the message fetch.  Each entry carries the AAD
    user id + display name but no email; we look up email best-effort
    via the user-profile endpoint (same pattern the sender path already
    uses at the "Fetch email from user profile if not in message"
    branch above).  Failures fall back to ``email=None`` so
    :func:`_resolve_roster_participants` still emits the entry with a
    ``None`` contact_id, letting Unity's unknown-contact flow decide.

    For private and shared channels this is the *only* participant
    signal available without ``ChannelMember.Read.All``, so we try
    reasonably hard to enrich it (bounded by mention count — rarely more
    than a handful per message).
    """
    mentions = message_data.get("mentions") or []
    if not mentions:
        return []

    out: list[dict] = []
    for mention in mentions:
        mentioned = mention.get("mentioned") or {}
        user = mentioned.get("user") or {}
        aad_id = user.get("id")
        display_name = user.get("displayName") or mention.get("mentionText") or ""
        if not aad_id:
            continue
        email: str | None = None
        try:
            data, _status = await _graph_get_json(
                f"https://graph.microsoft.com/v1.0/users/{aad_id}"
                f"?$select=mail,userPrincipalName",
                bearer,
                timeout=5.0,
            )
        except Exception:
            data = None
        if data:
            email = data.get("mail") or data.get("userPrincipalName") or None
        out.append(
            {
                "aad_user_id": aad_id,
                "email": email,
                "display_name": display_name,
                "tenant_id": None,
            },
        )
    return out


def _resolve_roster_participants(
    *,
    roster: list[dict],
    contacts: list[dict],
    assistant_email: str,
) -> list[dict]:
    """Resolve each roster member to ``contact_id`` where possible.

    Returns a list of participant dicts shaped for downstream Unity
    consumption::

        {"contact_id": int | None,
         "email": str | None,
         "display_name": str,
         "aad_user_id": str | None}

    Resolution order per member:

    1. Assistant's own mailbox → ``contact_id = 0`` (the Unity
       convention for "the assistant itself").
    2. Exact email match (case-insensitive) against any contact's
       ``email_address``.
    3. Name match via :func:`_match_contact_by_name` — only succeeds
       when exactly one contact's first/surname lines up, which is the
       same tolerance the sender path already uses for federated /
       consumer Teams users without a resolvable email.

    Members that can't be matched are emitted with ``contact_id=None``
    so Unity's ``_get_or_create_unknown_contact`` path can finish the
    resolve with the canonical contact creation semantics.  Members
    missing any usable email are emitted with ``email=None`` so Unity
    skips unknown-contact creation for them (creating a contact keyed
    on a synthetic ``{id}@teams`` placeholder would pollute the store).
    """
    from .helpers import _match_contact_by_name

    assistant_email_l = (assistant_email or "").lower()
    contacts_by_email = {
        (c.get("email_address") or "").lower(): c
        for c in contacts
        if c.get("email_address")
    }

    out: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for m in roster:
        email = m.get("email") or None
        email_l = (email or "").lower()
        display_name = m.get("display_name") or ""
        aad_id = m.get("aad_user_id")

        dedupe_key = ("email", email_l) if email_l else ("aad", aad_id or "")
        if dedupe_key in seen_keys or dedupe_key == ("aad", ""):
            continue
        seen_keys.add(dedupe_key)

        contact_id: int | None = None
        if email_l and email_l == assistant_email_l:
            contact_id = 0
        elif email_l and email_l in contacts_by_email:
            cid = contacts_by_email[email_l].get("contact_id")
            contact_id = int(cid) if cid is not None else None
        elif display_name:
            matched = _match_contact_by_name(display_name, contacts)
            if matched is not None:
                cid = matched.get("contact_id")
                contact_id = int(cid) if cid is not None else None

        out.append(
            {
                "contact_id": contact_id,
                "email": email,
                "display_name": display_name,
                "aad_user_id": aad_id,
            },
        )

    if not any(p.get("contact_id") == 0 for p in out):
        out.append(
            {
                "contact_id": 0,
                "email": assistant_email,
                "display_name": "",
                "aad_user_id": None,
            },
        )
    return out


async def _handle_teams_lifecycle(
    item: dict,
    http_client: httpx.AsyncClient,
) -> None:
    """Process a single Graph lifecycle notification item.

    The unified Teams watcher is delegated-only and creates a per-user
    chats subscription (``/users/{id}/chats/getAllMessages``) plus one
    per-channel subscription for every channel in every team the user
    belongs to.  Graph posts ``lifecycleEvent`` items with one of:

    - ``subscriptionRemoved`` / ``missed`` — re-bootstrap by POSTing
      ``/teams/watch``; that endpoint deletes any owned stale subs and
      re-creates them all.
    - ``reauthorizationRequired`` — also handled by re-POSTing
      ``/teams/watch``.  Delegated subs rarely fire this event (the
      30 min scheduler tick handles renewal in the common case), but
      we treat it the same way: rebuild from scratch instead of
      PATCHing, because the user may have rotated tokens between the
      old sub creation and now.

    Ownership is verified via ``clientState`` (``{secret}::{email}``)
    to avoid touching subscriptions that belong to other tenants or
    apps sharing the same endpoint.
    """
    event = item.get("lifecycleEvent")
    client_state = item.get("clientState", "")

    expected_secret = os.environ.get("TEAMS_WEBHOOK_SECRET", "")
    if not expected_secret or "::" not in client_state:
        logger.info(f"lifecycle: dropping item with invalid clientState ({event})")
        return
    secret, _, assistant_email = client_state.partition("::")
    if secret != expected_secret or not assistant_email:
        logger.info(f"lifecycle: dropping item with wrong secret ({event})")
        return

    if event not in ("subscriptionRemoved", "missed", "reauthorizationRequired"):
        logger.info(f"lifecycle: unhandled event {event!r}")
        return

    try:
        resp = await http_client.post(
            f"{SETTINGS.comms_url}/teams/watch",
            json={"primary_email": assistant_email},
            headers={
                "Authorization": f"Bearer {SETTINGS.orchestra_admin_key}",
            },
            timeout=2.0,
        )
        logger.info(
            f"lifecycle: rebuild /teams/watch for "
            f"{_redact_email(assistant_email)} ({event}) returned "
            f"{resp.status_code}",
        )
    except Exception as e:
        logger.info(f"lifecycle: rebuild request sent (timeout ok): {e}")


@app.post("/microsoft/router")
async def microsoft_router(request: Request):
    """
    Router for Microsoft Graph webhook notifications.
    Returns 200 immediately to satisfy Microsoft's 3-second timeout,
    then routes notifications to appropriate processors via HTTP.
    """
    logger.info("microsoft_router function started")

    # Handle validation token (required for subscription setup)
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        logger.info("returning validation token")
        return Response(content=validation_token, media_type="text/plain")

    # Parse notifications
    body = await request.body()
    json_body = json.loads(body)
    notifications = json_body.get("value", [])
    logger.info(f"routing {len(notifications)} notification(s)")

    # Route each notification to appropriate processor
    adapters_url = os.getenv("UNITY_ADAPTERS_URL", "http://localhost:8001")

    async with httpx.AsyncClient() as client:
        for notification in notifications:
            # Lifecycle notifications share the same URL as change
            # notifications (app-only Teams subs use the same endpoint for
            # both); we identify them by the ``lifecycleEvent`` field and
            # handle them inline rather than forwarding to /chat/teams.
            if notification.get("lifecycleEvent"):
                await _handle_teams_lifecycle(notification, client)
                continue

            resource = notification.get("resource", "")

            # Route based on resource type
            if "/Messages/" in resource and "/chats/" not in resource.lower():
                # Outlook email messages (users/{id}/mailFolders/inbox/messages)
                target = f"{adapters_url}/email/outlook"
            elif (
                "/chats/" in resource.lower()
                or "chats(" in resource.lower()
                or "getAllMessages" in resource
                or ("teams(" in resource.lower() and "channels(" in resource.lower())
            ):
                # Teams messages - both chat and channel go to same handler.
                # Graph delivers the resource in either path form
                # (/chats/{id}/messages/{id}) or OData form
                # (chats('id')/messages('id')); channel notifications come as
                # teams('id')/channels('id')/messages('id').
                target = f"{adapters_url}/chat/teams"
            else:
                logger.info(f"unknown resource type: {resource}")
                continue

            logger.info(f"routing to {target}")
            try:
                await client.post(target, json=notification, timeout=1.0)
            except Exception as e:
                # Log but don't fail - the request was sent
                logger.info(f"request sent (may have timed out on response): {e}")

    logger.info("returning 200 OK")
    return Response(content="OK", status_code=200)


# =============================================================================
# BYOD Helpers
# =============================================================================


async def _register_teams_user_id_contact(
    *,
    assistant_id: str,
    api_key: str,
    sender_id: str,
    sender_name: str,
    synthetic_email: str,
) -> None:
    """Cache a (Teams sender_id → contact) mapping in Orchestra.

    Called the first time we successfully resolve a federated /
    anonymous-guest / consumer-MSA Teams sender by name.  Subsequent
    messages from the same ``sender_id`` can short-circuit the
    name-fallback path; the synthetic ``{sender_id}@teams`` email
    becomes a stable lookup key.

    Best-effort: failures are logged and swallowed so they never block
    the inbound message that triggered them.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{SETTINGS.orchestra_url}/assistant/{assistant_id}/contact",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "contact_type": "teams_user_id",
                    "provisioned_by": "system",
                    "contact_value": synthetic_email,
                    "display_name": sender_name,
                    "external_id": sender_id,
                },
                timeout=30,
            )
            if response.status_code >= 400:
                logger.info(
                    "teams_user_id cache write returned %s: %s",
                    response.status_code,
                    response.text[:200],
                )
    except Exception as e:
        logger.info(f"teams_user_id cache write failed (non-fatal): {e}")


async def _register_byod_email_contact(
    *,
    assistant_id: str,
    email: str,
    provider: str,
    api_key: str,
) -> None:
    """Create an AssistantContact row in Orchestra for a BYOD email.

    Calls Orchestra's admin endpoint to upsert the contact with
    ``provisioned_by=user`` so the platform knows it doesn't own the
    underlying mailbox.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{SETTINGS.orchestra_url}/assistant/{assistant_id}/contact",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "contact_type": "email",
                "provisioned_by": "user",
                "contact_value": email,
                "email_provider": provider,
            },
            timeout=30,
        )
        if response.status_code >= 400:
            logger.error(
                "Orchestra contact creation returned %s: %s",
                response.status_code,
                response.text,
            )
            raise Exception(
                f"Orchestra returned {response.status_code}: {response.text}",
            )


@app.get("/microsoft/auth/callback")
async def microsoft_oauth_callback(request: Request):
    """OAuth callback for Microsoft — handles both enterprise and BYOD flows.

    **Enterprise flow** (existing): state contains ``assistant_email``,
    ``tenant_id``, ``client_id``.  The ``AZURE_CLIENT_SECRET`` is read
    from the assistant's secrets in Orchestra.

    **BYOD flow**: state contains ``assistant_id`` and ``byod: true``.
    Credentials come from platform env vars (``MS365_BYOD_CLIENT_ID``,
    ``MS365_BYOD_CLIENT_SECRET``).  After token exchange the user's
    email is discovered via ``GET /me`` and an ``AssistantContact`` is
    created in Orchestra with ``provisioned_by=user``.

    See ``docs/MICROSOFT_OAUTH_SETUP_GUIDE.md`` for enterprise setup.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    error_description = request.query_params.get("error_description")

    if error:
        logger.error(f"OAuth error: {error} - {error_description}")
        return Response(
            content=f"OAuth error: {error}: {error_description}",
            status_code=400,
        )

    if not code:
        return Response(content="Missing authorization code", status_code=400)

    # ------------------------------------------------------------------
    # Decode + verify state
    # ------------------------------------------------------------------
    if not state:
        return Response(content="Missing state parameter", status_code=400)

    if not SETTINGS.oauth_state_signing_key:
        return Response(
            content="OAUTH_STATE_SIGNING_KEY not configured",
            status_code=500,
        )

    try:
        state_data = verify_oauth_state(state, SETTINGS.oauth_state_signing_key)
    except OAuthStateError as exc:
        return Response(content=str(exc), status_code=400)

    is_byod = state_data.get("byod", False)
    redirect_after = state_data.get("redirect_after")
    features = state_data.get("features", ["email"])

    # ------------------------------------------------------------------
    # Resolve assistant + credentials
    # ------------------------------------------------------------------
    if is_byod:
        raw_assistant_id = state_data.get("assistant_id")
        if not raw_assistant_id:
            return Response(
                content="Missing assistant_id in BYOD state",
                status_code=400,
            )

        assistant = get_assistant(assistant_id=str(raw_assistant_id))
        if not assistant or not assistant.get("assistant_id"):
            return Response(
                content=f"Assistant not found: {raw_assistant_id}",
                status_code=400,
            )

        client_id = SETTINGS.ms365_byod_client_id
        client_secret = os.environ.get("MS365_BYOD_CLIENT_SECRET", "")
        tenant_id = "common"

        if not client_id or not client_secret:
            return Response(
                content="MS365 BYOD app credentials not configured on the platform",
                status_code=500,
            )
    else:
        # Enterprise flow — existing behaviour
        tenant_id = state_data.get("tenant_id")
        client_id = state_data.get("client_id")
        assistant_email = state_data.get("assistant_email")

        if not tenant_id or not client_id:
            return Response(
                content="Missing tenant_id or client_id in state",
                status_code=400,
            )
        if not assistant_email:
            return Response(
                content="Missing assistant_email in state",
                status_code=400,
            )

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
                content="AZURE_CLIENT_SECRET not found in assistant secrets. "
                "Add it to the assistant configuration.",
                status_code=400,
            )

    redirect_uri = os.getenv("UNITY_ADAPTERS_URL", "") + "/microsoft/auth/callback"

    # ------------------------------------------------------------------
    # Exchange code → tokens
    # ------------------------------------------------------------------
    try:
        tokens = await exchange_microsoft_code_for_tokens(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except Exception as e:
        logger.error(f"Token exchange failed: {e}")
        return Response(content=f"Token exchange failed: {e}", status_code=400)

    # Discover the authenticated user's email
    try:
        logger.info("Token exchange successful")
        user_info = await get_microsoft_user_info(tokens["access_token"])
        user_email = user_info.get("mail") or user_info.get("userPrincipalName")
    except Exception as e:
        logger.error(f"Failed to get user info: {e}")
        return Response(content=f"Failed to get user info: {e}", status_code=400)

    if not user_email:
        return Response(
            content="Could not determine user email from token",
            status_code=400,
        )

    # ------------------------------------------------------------------
    # Store tokens + granted scopes
    # ------------------------------------------------------------------
    from common.scopes import build_scope_string

    granted_scopes = build_scope_string("microsoft", features) if is_byod else ""
    assistant_id = assistant["assistant_id"]
    api_key = assistant["api_key"]
    stored = await store_microsoft_tokens(
        assistant_id=assistant_id,
        new_secrets=tokens,
        api_key=api_key,
        granted_scopes=granted_scopes,
    )

    # ------------------------------------------------------------------
    # BYOD: post-OAuth actions driven by state from Orchestra
    # ------------------------------------------------------------------
    if is_byod:
        actions = state_data.get("actions", {})

        if actions.get("register_email_contact"):
            try:
                await _register_byod_email_contact(
                    assistant_id=assistant_id,
                    email=user_email,
                    provider="microsoft_365",
                    api_key=api_key,
                )
            except Exception as e:
                logger.error(f"Failed to register BYOD email contact: {e}")

        if actions.get("setup_email_watch"):
            try:
                async with httpx.AsyncClient() as http_client:
                    await http_client.post(
                        f"{SETTINGS.comms_url}/outlook/watch",
                        json={"primary_email": user_email},
                        headers={
                            "Authorization": f"Bearer {SETTINGS.orchestra_admin_key}",
                        },
                        timeout=30,
                    )
            except Exception as e:
                logger.error(f"Failed to set up Outlook watch after BYOD OAuth: {e}")

        if actions.get("setup_teams_watch"):
            try:
                async with httpx.AsyncClient() as http_client:
                    await http_client.post(
                        f"{SETTINGS.comms_url}/teams/watch",
                        json={"primary_email": user_email},
                        headers={
                            "Authorization": f"Bearer {SETTINGS.orchestra_admin_key}",
                        },
                        timeout=30,
                    )
            except Exception as e:
                logger.error(f"Failed to set up Teams watch after BYOD OAuth: {e}")

    logger.info(
        f"OAuth complete for {_redact_email(user_email)} "
        f"(assistant id: {assistant_id}, byod={is_byod}), stored={stored}, "
        f"features={features}, granted_scopes={granted_scopes[:80]}",
    )

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


# =========================================================================
# Google token revocation (called by Orchestra before scope-reduction re-auth)
# =========================================================================


@app.post("/google/revoke", dependencies=[Depends(require_admin_key)])
async def google_revoke(request: Request):
    """Revoke a Google OAuth token and clear related assistant secrets.

    Called by Orchestra's ``POST /assistant/{id}/connect`` when the user
    reduces their granted scopes.  Google doesn't support partial
    revocation, so the entire grant is revoked and the user re-authorizes
    with the reduced scope set.

    Request body::

        { "assistant_id": "...", "token": "the-access-token" }
    """
    data = await request.json()
    token = data.get("token")
    assistant_id = data.get("assistant_id")

    if not token:
        return Response(content="Missing token", status_code=400)

    # Revoke with Google
    async with httpx.AsyncClient() as http_client:
        resp = await http_client.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        revoked = resp.status_code == 200
        if not revoked:
            logger.warning(
                "Google token revocation returned %s: %s",
                resp.status_code,
                resp.text,
            )

    # Clear stale secrets so revoked tokens don't linger
    if assistant_id:
        assistant = get_assistant(assistant_id=str(assistant_id))
        if assistant and assistant.get("api_key"):
            api_key = assistant["api_key"]
            secrets_to_clear = [
                "GOOGLE_ACCESS_TOKEN",
                "GOOGLE_REFRESH_TOKEN",
                "GOOGLE_TOKEN_EXPIRES_AT",
                "GOOGLE_GRANTED_SCOPES",
            ]
            async with httpx.AsyncClient() as http_client:
                for name in secrets_to_clear:
                    await http_client.delete(
                        f"{SETTINGS.orchestra_url}/assistant/{assistant_id}/secret/{name}",
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=10,
                    )

    return Response(
        content=json.dumps({"revoked": revoked}),
        media_type="application/json",
    )


@app.get("/google/auth/callback")
async def google_oauth_callback(request: Request):
    """OAuth callback for Google — BYOD access.

    State contains ``assistant_id``, ``features``, and ``redirect_after``.
    Credentials come from platform env vars (``GOOGLE_OAUTH_CLIENT_ID``,
    ``GOOGLE_OAUTH_CLIENT_SECRET``).  After token exchange the user's
    email is discovered, granted scopes are recorded, and actions
    (contact registration, email watch) are taken based on which features
    were actually granted.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        error_desc = request.query_params.get("error_description", "")
        logger.error(f"Google OAuth error: {error} — {error_desc}")
        return Response(content=f"OAuth error: {error} — {error_desc}", status_code=400)

    if not code:
        return Response(content="Missing authorization code", status_code=400)

    if not state:
        return Response(content="Missing state parameter", status_code=400)

    if not SETTINGS.oauth_state_signing_key:
        return Response(
            content="OAUTH_STATE_SIGNING_KEY not configured",
            status_code=500,
        )

    try:
        state_data = verify_oauth_state(state, SETTINGS.oauth_state_signing_key)
    except OAuthStateError as exc:
        return Response(content=str(exc), status_code=400)

    raw_assistant_id = state_data.get("assistant_id")
    redirect_after = state_data.get("redirect_after")
    features = state_data.get("features", ["email"])

    if not raw_assistant_id:
        return Response(content="Missing assistant_id in state", status_code=400)

    assistant = get_assistant(assistant_id=str(raw_assistant_id))
    if not assistant or not assistant.get("assistant_id"):
        return Response(
            content=f"Assistant not found: {raw_assistant_id}",
            status_code=400,
        )

    client_id = SETTINGS.google_oauth_client_id
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return Response(
            content="Google OAuth app credentials not configured on the platform",
            status_code=500,
        )

    redirect_uri = os.getenv("UNITY_ADAPTERS_URL", "") + "/google/auth/callback"

    # Exchange code → tokens
    try:
        tokens = await exchange_google_code_for_tokens(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except Exception as e:
        logger.error(f"Google token exchange failed: {e}")
        return Response(content=f"Token exchange failed: {e}", status_code=400)

    # Discover the authenticated user's email
    try:
        logger.info("Google token exchange successful")
        user_info = await get_google_user_info(tokens["access_token"])
        user_email = user_info.get("email")
    except Exception as e:
        logger.error(f"Failed to get Google user info: {e}")
        return Response(content=f"Failed to get user info: {e}", status_code=400)

    if not user_email:
        return Response(
            content="Could not determine user email from token",
            status_code=400,
        )

    # Store tokens + granted scopes
    from common.scopes import build_scope_string

    granted_scopes = build_scope_string("google", features)
    assistant_id = assistant["assistant_id"]
    api_key = assistant["api_key"]
    stored = await store_google_tokens(
        assistant_id=assistant_id,
        new_secrets=tokens,
        api_key=api_key,
        granted_scopes=granted_scopes,
    )

    actions = state_data.get("actions", {})

    if actions.get("register_email_contact"):
        try:
            await _register_byod_email_contact(
                assistant_id=assistant_id,
                email=user_email,
                provider="google_workspace",
                api_key=api_key,
            )
        except Exception as e:
            logger.error(f"Failed to register BYOD Gmail contact: {e}")

    if actions.get("setup_email_watch"):
        try:
            async with httpx.AsyncClient() as http_client:
                await http_client.post(
                    f"{SETTINGS.comms_url}/gmail/watch",
                    json={"primary_email": user_email},
                    headers={
                        "Authorization": f"Bearer {SETTINGS.orchestra_admin_key}",
                    },
                    timeout=30,
                )
        except Exception as e:
            logger.error(f"Failed to set up Gmail watch after BYOD OAuth: {e}")

    logger.info(
        f"Google OAuth complete for {_redact_email(user_email)} "
        f"(assistant id: {assistant_id}), stored={stored}, "
        f"features={features}, granted_scopes={granted_scopes[:80]}",
    )

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


@app.post("/scheduled/email-watches", dependencies=[Depends(require_admin_key)])
def scheduled_email_watches(payload: ScheduledPayload):
    """
    Cloud Run endpoint that renews email watches for all assistants.
    Automatically detects provider based on secrets:
    - If MICROSOFT_ACCESS_TOKEN exists -> Outlook watch
    - Otherwise -> Gmail watch
    """
    admin_key = SETTINGS.orchestra_admin_key
    if not admin_key:
        return Response(content="ORCHESTRA_ADMIN_KEY not configured", status_code=500)

    # Fetch all assistants with their secrets
    if not payload.test:
        try:
            response = requests.get(
                f"{SETTINGS.orchestra_url}/admin/assistant",
                headers={"Authorization": f"Bearer {admin_key}"},
            )
            if response.status_code != 200:
                return Response(
                    content=f"Failed to get assistants: {response.text}",
                    status_code=500,
                )
            all_assistants = response.json().get("info", [])
        except Exception as e:
            logger.error(f"Failed to get assistants: {e}")
            return Response(content=f"Failed to get assistants: {e}", status_code=500)
    else:
        all_assistants = [{"email": "default-test-assistant@unify.ai", "secrets": {}}]

    logger.info(f"Processing {len(all_assistants)} assistants for email watch renewal")

    results = {"gmail": [], "outlook": []}

    for assistant in all_assistants:
        email = assistant.get("email")
        if not email:
            continue

        # Determine provider from the assistant record.  Fall back to
        # token-sniffing for assistants that predate the email_provider field.
        email_provider = assistant.get("email_provider")
        if not email_provider:
            has_ms_token = bool(
                assistant.get("secrets", {}).get("MICROSOFT_ACCESS_TOKEN"),
            )
            email_provider = "microsoft_365" if has_ms_token else "google_workspace"

        use_outlook = email_provider == "microsoft_365"

        try:
            if use_outlook:
                watch_response = requests.post(
                    f"{SETTINGS.comms_url}/outlook/watch",
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
                logger.info(
                    f"Outlook watch for {_redact_email(email)}: {result.get('success', False)}",
                )
            else:
                watch_response = requests.post(
                    f"{SETTINGS.comms_url}/gmail/watch",
                    json={
                        "primary_email": email,
                        "topic_name": SETTINGS.gmail_topic,
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
                logger.info(
                    f"Gmail watch for {_redact_email(email)}: {result.get('success', False)}",
                )

        except Exception as e:
            error_msg = f"Error renewing watch for {_redact_email(email)}: {e}"
            logger.error(error_msg)
            provider = "outlook" if use_outlook else "gmail"
            results[provider].append(
                {"email": email, "success": False, "error": error_msg},
            )

    # Renew policy assistant (Gmail-based, skip only in test mode)
    if not payload.test:
        try:
            response = requests.post(
                f"{SETTINGS.comms_url}/gmail/watch",
                json={
                    "primary_email": "mh-policies@unify.ai",
                    "topic_name": "intranet",
                },
                headers={"Authorization": f"Bearer {admin_key}"},
                timeout=30,
            ).json()
            results["gmail"].append({"email": "mh-policies@unify.ai", **response})
            logger.info(f"Renewed policy assistant: {response}")
        except Exception as e:
            logger.error(f"Error renewing policy assistant: {e}")

    logger.info(
        f"Email watch renewal complete: {len(results['gmail'])} Gmail, {len(results['outlook'])} Outlook",
    )
    return results


@app.post("/scheduled/microsoft-tokens", dependencies=[Depends(require_admin_key)])
def scheduled_microsoft_tokens(payload: ScheduledPayload):
    """
    Cloud Run endpoint that refreshes Microsoft OAuth tokens for all assistants.
    Should be scheduled to run every 30-45 minutes to keep access tokens fresh.

    Fetches all assistants in a single call and only processes those with
    Microsoft tokens configured in their secrets.
    """
    admin_key = SETTINGS.orchestra_admin_key
    if not admin_key:
        return Response(content="ORCHESTRA_ADMIN_KEY not configured", status_code=500)

    # Get all assistants in a single call (no params = all assistants)
    try:
        response = requests.get(
            f"{SETTINGS.orchestra_url}/admin/assistant",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        if response.status_code != 200:
            return Response(
                content=f"Failed to get assistants: {response.text}",
                status_code=500,
            )
        all_assistants = response.json().get("info", [])
    except Exception as e:
        logger.error(f"Failed to get assistants: {e}")
        return Response(content=f"Failed to get assistants: {e}", status_code=500)

    logger.info(f"Fetched {len(all_assistants)} assistants")
    results = {"refreshed": [], "failed": []}

    for assistant in all_assistants:
        email = assistant.get("email", "unknown")
        assistant_id = assistant.get("agent_id")

        # Skip test in non-test mode
        if payload.test and email != "default-test-assistant@unify.ai":
            continue

        # Skip if no Microsoft tokens configured
        secrets = assistant.get("secrets", {})
        if not secrets:
            continue
        access_token = secrets.get("MICROSOFT_ACCESS_TOKEN")
        refresh_token = secrets.get("MICROSOFT_REFRESH_TOKEN")
        if not access_token or not refresh_token:
            continue

        # Get required credentials for refresh.
        # Enterprise flow: per-assistant Azure credentials.
        # BYOD fallback: platform-level multi-tenant app.
        tenant_id = secrets.get("AZURE_TENANT_ID")
        client_id = secrets.get("AZURE_CLIENT_ID")
        client_secret = secrets.get("AZURE_CLIENT_SECRET")
        is_byod = False
        if not all([tenant_id, client_id, client_secret]):
            byod_client_id = SETTINGS.ms365_byod_client_id
            byod_client_secret = os.environ.get("MS365_BYOD_CLIENT_SECRET", "")
            if byod_client_id and byod_client_secret:
                tenant_id = "common"
                client_id = byod_client_id
                client_secret = byod_client_secret
                is_byod = True
            else:
                results["failed"].append(
                    {"email": email, "error": "Missing Azure credentials"},
                )
                continue

        # BYOD: use the stored granted scopes so scope reduction is
        # durable across refreshes.  Enterprise: use .default (admin
        # controls permissions at the app registration level).
        if is_byod and secrets.get("MICROSOFT_GRANTED_SCOPES"):
            refresh_scope = secrets["MICROSOFT_GRANTED_SCOPES"]
        else:
            refresh_scope = "https://graph.microsoft.com/.default offline_access"

        try:
            token_resp = requests.post(
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    "scope": refresh_scope,
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

            api_key = assistant.get("api_key")

            # Store updated tokens
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
                    f"{SETTINGS.orchestra_url}/assistant/{assistant_id}/secret/{secret_name}",
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
            logger.info(f"Refreshed Microsoft token for {_redact_email(email)}")

        except Exception as e:
            results["failed"].append({"email": email, "error": str(e)})
            logger.error(f"Error refreshing token for {_redact_email(email)}: {e}")

    logger.info(
        f"Microsoft token refresh complete: "
        f"{len(results['refreshed'])} refreshed, {len(results['failed'])} failed",
    )
    return results


@app.post("/scheduled/google-tokens", dependencies=[Depends(require_admin_key)])
def scheduled_google_tokens(payload: ScheduledPayload):
    """Refresh Google OAuth tokens for all BYOD Gmail assistants.

    Should be scheduled to run every 30 minutes (access tokens expire
    in ~1 hour).  Only processes assistants that have
    ``GOOGLE_ACCESS_TOKEN`` and ``GOOGLE_REFRESH_TOKEN`` in secrets.
    """
    admin_key = SETTINGS.orchestra_admin_key
    if not admin_key:
        return Response(content="ORCHESTRA_ADMIN_KEY not configured", status_code=500)

    client_id = SETTINGS.google_oauth_client_id
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return Response(
            content="GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET not configured",
            status_code=500,
        )

    try:
        response = requests.get(
            f"{SETTINGS.orchestra_url}/admin/assistant",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        if response.status_code != 200:
            return Response(
                content=f"Failed to get assistants: {response.text}",
                status_code=500,
            )
        all_assistants = response.json().get("info", [])
    except Exception as e:
        logger.error(f"Failed to get assistants: {e}")
        return Response(content=f"Failed to get assistants: {e}", status_code=500)

    logger.info(f"Checking {len(all_assistants)} assistants for Google token refresh")
    results = {"refreshed": [], "failed": []}

    for assistant in all_assistants:
        email = assistant.get("email", "unknown")
        assistant_id = assistant.get("agent_id")

        if payload.test and email != "default-test-assistant@unify.ai":
            continue

        secrets = assistant.get("secrets", {})
        if not secrets:
            continue
        access_token = secrets.get("GOOGLE_ACCESS_TOKEN")
        refresh_token = secrets.get("GOOGLE_REFRESH_TOKEN")
        if not access_token or not refresh_token:
            continue

        try:
            token_resp = requests.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
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

            api_key = assistant.get("api_key")

            secrets_to_store = {
                "GOOGLE_ACCESS_TOKEN": new_tokens["access_token"],
                "GOOGLE_TOKEN_EXPIRES_AT": expires_at,
            }
            if new_tokens.get("refresh_token"):
                secrets_to_store["GOOGLE_REFRESH_TOKEN"] = new_tokens["refresh_token"]

            for secret_name, secret_value in secrets_to_store.items():
                response = requests.put(
                    f"{SETTINGS.orchestra_url}/assistant/{assistant_id}/secret/{secret_name}",
                    json={"secret_value": secret_value},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if response.status_code != 200:
                    results["failed"].append(
                        {
                            "email": email,
                            "error": f"Failed to store {secret_name}: {response.text}",
                        },
                    )
                    continue

            results["refreshed"].append(email)
            logger.info(f"Refreshed Google token for {_redact_email(email)}")

        except Exception as e:
            results["failed"].append({"email": email, "error": str(e)})
            logger.error(
                f"Error refreshing Google token for {_redact_email(email)}: {e}"
            )

    logger.info(
        f"Google token refresh complete: "
        f"{len(results['refreshed'])} refreshed, {len(results['failed'])} failed",
    )
    return results


@app.post("/scheduled/teams-watches", dependencies=[Depends(require_admin_key)])
def scheduled_teams_watches(payload: ScheduledPayload):
    """
    Cloud Run endpoint that renews the unified Teams subscriptions
    (per-user chats + per-channel) for every Microsoft-365 assistant.
    Teams subscriptions expire after 60 minutes, so this runs every
    30 mins.

    A single POST to ``/teams/watch`` per assistant rebuilds everything:
    the chats sub plus one sub per channel across every joined team.
    Each renewal re-enumerates channels so newly joined teams/channels
    are picked up automatically.

    The comms service requires a delegated token for every mailbox in
    this codepath; us-provisioned mailboxes get one via ROPC at
    ``/outlook/create``.  Mailboxes without a stored token surface as
    ``no_token`` in the results and need a re-provision or BYOD OAuth.
    """
    admin_key = SETTINGS.orchestra_admin_key
    if not admin_key:
        return Response(content="ORCHESTRA_ADMIN_KEY not configured", status_code=500)

    if not payload.test:
        try:
            response = requests.get(
                f"{SETTINGS.orchestra_url}/admin/assistant",
                headers={"Authorization": f"Bearer {admin_key}"},
            )
            if response.status_code != 200:
                return Response(
                    content=f"Failed to get assistants: {response.text}",
                    status_code=500,
                )
            all_assistants = response.json().get("info", [])
        except Exception as e:
            logger.error(f"Failed to get assistants: {e}")
            return Response(content=f"Failed to get assistants: {e}", status_code=500)
    else:
        all_assistants = [
            {
                "email": "default-test-assistant@unify.ai",
                "secrets": {"MICROSOFT_ACCESS_TOKEN": "test"},
            },
        ]

    logger.info(f"Processing {len(all_assistants)} assistants for Teams watch renewal")

    results = {
        "renewed": [],
        "channel_failures": [],
        "no_token": [],
        "timed_out": [],
        "failed": [],
    }

    for assistant in all_assistants:
        email = assistant.get("email")
        if not email:
            continue

        secrets = assistant.get("secrets", {})
        access_token = secrets.get("MICROSOFT_ACCESS_TOKEN")

        email_provider = assistant.get("email_provider")
        if not email_provider:
            email_provider = "microsoft_365" if access_token else "google_workspace"
        if email_provider != "microsoft_365":
            continue

        try:
            watch_response = requests.post(
                f"{SETTINGS.comms_url}/teams/watch",
                json={"primary_email": email},
                headers={"Authorization": f"Bearer {admin_key}"},
                timeout=30,
            )
            if watch_response.status_code == 409:
                results["no_token"].append(
                    {"email": email, "detail": watch_response.text[:300]},
                )
                continue
            if watch_response.status_code != 200:
                results["failed"].append(
                    {
                        "email": email,
                        "status": watch_response.status_code,
                        "error": watch_response.text[:500],
                    },
                )
                continue

            result = watch_response.json()
            if result.get("success"):
                results["renewed"].append({"email": email, **result})
            else:
                results["failed"].append({"email": email, **result})

            # Per-channel failures are expected occasionally (transient
            # 429s, unusual channel types).  Track counts per assistant
            # so dashboards can surface tenants with chronic failures
            # without drowning on per-sub entries.
            if result.get("channel_failures"):
                results["channel_failures"].append(
                    {
                        "email": email,
                        "failed": result["channel_failures"],
                        "total": result.get("channel_count", 0),
                    },
                )

        except requests.exceptions.Timeout:
            logger.info(
                f"Teams watch for {_redact_email(email)}: timed out "
                "(request may still succeed)",
            )
            results["timed_out"].append({"email": email, "status": "timeout"})
        except Exception as e:
            error_msg = f"Error renewing Teams watch for {_redact_email(email)}: {e}"
            logger.error(error_msg, exc_info=True)
            results["failed"].append(
                {"email": email, "success": False, "error": error_msg},
            )

    logger.info(
        f"Teams watch renewal complete: "
        f"{len(results['renewed'])} renewed, "
        f"{len(results['channel_failures'])} with channel_failures, "
        f"{len(results['no_token'])} no_token, "
        f"{len(results['timed_out'])} timed_out, "
        f"{len(results['failed'])} failed",
    )
    return results


@app.post("/scheduled/infra/maintenance", dependencies=[Depends(require_admin_key)])
def scheduled_infra_maintenance():
    """Unified infrastructure maintenance sweep.

    Runs hourly via Cloud Scheduler.  Consolidates container pool
    replenishment, excess-idle cleanup, stale-job expiry, orphaned-VM
    reconciliation, quarantined-VM purge, VM pool health (scrub +
    probe + replenish), and bounded terminal AssistantSession pruning
    into a single scheduled endpoint so runtime cleanup concerns live
    in one place.
    """
    results: dict = {}
    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}

    # 1 — Replenish idle container pool (refresh=True rotates to latest image)
    try:
        results["pool_replenish"] = replenish_idle_pool(refresh=True)
    except Exception as exc:
        logger.exception("maintenance: pool replenish failed")
        results["pool_replenish_error"] = str(exc)

    # 2 — Delete excess idle containers
    try:
        results["pool_cleanup"] = cleanup_idle_pool()
    except Exception as exc:
        logger.exception("maintenance: pool cleanup failed")
        results["pool_cleanup_error"] = str(exc)

    # 3 — Stop stale assistant runtimes (running >12 h)
    try:
        results["stale_jobs"] = expire_all_stale_jobs(max_age_hours=12)
    except Exception as exc:
        logger.exception("maintenance: stale job expiry failed")
        results["stale_jobs_error"] = str(exc)

    # 4 — Release VMs assigned to assistants that no longer have running jobs
    for vm_type in SUPPORTED_POOL_VM_TYPES:
        key = f"orphaned_vms_{vm_type}"
        try:
            resp = requests.post(
                f"{SETTINGS.comms_url}/infra/vm/pool/reconcile-orphans",
                params={"vm_type": vm_type},
                headers=headers,
                timeout=60,
            )
            if resp.status_code == 200:
                results[key] = resp.json()
        except Exception as exc:
            logger.exception("maintenance: orphan VM reconcile failed for %s", vm_type)
            results[f"{key}_error"] = str(exc)

    # 5 — Delete quarantined VMs so replenish_pool can create fresh replacements
    for vm_type in SUPPORTED_POOL_VM_TYPES:
        key = f"quarantined_vms_{vm_type}"
        try:
            resp = requests.post(
                f"{SETTINGS.comms_url}/infra/vm/pool/purge-quarantined",
                params={"vm_type": vm_type},
                headers=headers,
                timeout=60,
            )
            if resp.status_code == 200:
                results[key] = resp.json()
        except Exception as exc:
            logger.exception("maintenance: quarantine purge failed for %s", vm_type)
            results[f"{key}_error"] = str(exc)

    # 6 — VM pool health: scrub inconsistent labels, probe idle VMs,
    #     quarantine unhealthy ones, and replenish to target capacity.
    for vm_type in SUPPORTED_POOL_VM_TYPES:
        key = f"vm_rebalance_{vm_type}"
        try:
            resp = requests.post(
                f"{SETTINGS.comms_url}/infra/vm/pool/rebalance",
                params={"vm_type": vm_type},
                headers=headers,
                timeout=120,
            )
            if resp.status_code == 200:
                results[key] = resp.json()
        except Exception as exc:
            logger.exception("maintenance: VM rebalance failed for %s", vm_type)
            results[f"{key}_error"] = str(exc)

    # 7 — Delete terminal AssistantSession CRs whose runtime is already gone.
    try:
        resp = requests.post(
            f"{SETTINGS.comms_url}/infra/sessions/prune-terminal",
            headers=headers,
            timeout=120,
        )
        if resp.status_code == 200:
            results["assistant_session_prune"] = resp.json()
        else:
            results["assistant_session_prune_error"] = resp.text
    except Exception as exc:
        logger.exception("maintenance: terminal session prune failed")
        results["assistant_session_prune_error"] = str(exc)

    return results


# ---------------------------------------------------------------------------
# Individual infra utilities — NOT wired to Cloud Scheduler.
# Kept for manual invocation, debugging, and integration test helpers.
# ---------------------------------------------------------------------------


@app.post("/scheduled/jobs/create", dependencies=[Depends(require_admin_key)])
def scheduled_jobs_create(refresh: bool = False, extra_demand: int = 0):
    """Replenish idle container pool (utility — use /scheduled/infra/maintenance for cron)."""
    return replenish_idle_pool(refresh=refresh, extra_demand=extra_demand)


@app.post("/scheduled/jobs/cleanup", dependencies=[Depends(require_admin_key)])
def scheduled_jobs_cleanup():
    """Delete excess idle containers (utility — use /scheduled/infra/maintenance for cron)."""
    return cleanup_idle_pool()


@app.post("/scheduled/jobs/expire-stale", dependencies=[Depends(require_admin_key)])
def scheduled_jobs_expire_stale(max_age_hours: int = 12):
    """Stop stale assistant runtimes (utility — use /scheduled/infra/maintenance for cron)."""
    result = expire_all_stale_jobs(max_age_hours=max_age_hours)
    try:
        orphan_resp = requests.post(
            f"{SETTINGS.comms_url}/infra/vm/pool/reconcile-orphans",
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
            timeout=60,
        )
        if orphan_resp.status_code == 200:
            result["orphaned_vms"] = orphan_resp.json()
    except Exception as e:
        result["orphaned_vms_error"] = str(e)
    return Response(content=json.dumps(result), status_code=200)


@app.post("/scheduled/cert-renewal", dependencies=[Depends(require_admin_key)])
def scheduled_cert_renewal():
    """Proxy cert-renewal to the comms app where the module is available.

    The cert_renewal module lives in communication/infra/ which is only
    packaged in the comms Docker image, not the adapters image.
    """
    resp = requests.post(
        f"{SETTINGS.comms_url}/infra/cert-renewal",
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=120,
    )
    return resp.json()


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Unity Adapters server...")
    logger.info("Available endpoints:")
    logger.info("  Twilio:")
    logger.info("    - POST /twilio/call")
    logger.info("    - POST /twilio/call-status")
    logger.info("    - POST /twilio/sms")
    logger.info("    - POST /twilio/whatsapp")
    logger.info("    - POST /twilio/whatsapp-call")
    logger.info("    - POST /twilio/whatsapp-call-status")
    logger.info("  Unify:")
    logger.info("    - POST /unify/message")
    logger.info("    - POST /unify/meet")
    logger.info("  Unity:")
    logger.info("    - POST /unity/system-event")
    logger.info("    - POST /unity/pre-hire")
    logger.info("  Assistant:")
    logger.info("    - POST /assistant/wakeup")
    logger.info("    - POST /assistant/update")
    logger.info("  Email:")
    logger.info("    - POST /email/gmail")
    logger.info("    - POST /email/outlook")
    logger.info("  Microsoft:")
    logger.info("    - POST /microsoft/router")
    logger.info("    - GET  /microsoft/auth/callback")
    logger.info("  Scheduled:")
    logger.info("    - POST /scheduled/infra/maintenance")
    logger.info("    - POST /scheduled/email-watches")
    logger.info("    - POST /scheduled/cert-renewal")
    logger.info("Server running at: http://localhost:8080")

    uvicorn.run("adapters.main:app", host="0.0.0.0", port=8081, reload=True)
