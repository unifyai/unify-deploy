import base64
import logging
import json
import os
import random
import string
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from common.settings import SETTINGS
from communication.helpers import _lookup_assistant
from google.oauth2.credentials import Credentials as OAuthCredentials
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

router = APIRouter()
logger = logging.getLogger(__name__)


# Helpers
def _is_google_not_found_error(exc: Exception) -> bool:
    """Return True when a Google API error represents an already-missing user."""
    return isinstance(exc, HttpError) and getattr(exc.resp, "status", None) == 404


def _service_account_credentials(*, scopes: list[str], subject: str) -> Credentials:
    """Build delegated service-account credentials for Workspace operations."""
    creds_json = os.getenv("GCP_SA_KEY")
    if not creds_json:
        raise RuntimeError("GCP_SA_KEY must be set for Gmail operations")
    return Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=scopes,
        subject=subject,
    )


def _gmail_topic_path(topic_name: str | None = None) -> str:
    """Return the fully qualified Pub/Sub topic path used by Gmail watches."""
    return (
        f"projects/{SETTINGS.gcp_project_id}/topics/"
        f"{topic_name or SETTINGS.gmail_topic}"
    )


def get_admin_service():
    """Build a Directory API client with domain-wide delegation."""
    creds = _service_account_credentials(
        scopes=["https://www.googleapis.com/auth/admin.directory.user"],
        subject=SETTINGS.workspace_admin_subject,
    )
    return build("admin", "directory_v1", credentials=creds)


_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


async def get_gmail_service_async(sender_email: str):
    """Build a Gmail API client, preferring BYOD OAuth tokens.

    For user-granted (BYOD) accounts the assistant has a
    ``GOOGLE_ACCESS_TOKEN`` secret.  For platform-managed Workspace
    accounts we fall back to service-account delegation.
    """
    try:
        assistant = await _lookup_assistant(sender_email)
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise
        logger.warning(
            "Assistant lookup failed for %s (status %s), falling back to SA",
            sender_email,
            exc.status_code,
        )
        creds = _service_account_credentials(scopes=_GMAIL_SCOPES, subject=sender_email)
        return build("gmail", "v1", credentials=creds)
    except Exception:
        logger.warning(
            "Failed to look up assistant for %s, falling back to SA", sender_email
        )
        creds = _service_account_credentials(scopes=_GMAIL_SCOPES, subject=sender_email)
        return build("gmail", "v1", credentials=creds)

    access_token = assistant.get("secrets", {}).get("GOOGLE_ACCESS_TOKEN")
    if access_token:
        creds = OAuthCredentials(token=access_token)
        return build("gmail", "v1", credentials=creds)

    creds = _service_account_credentials(scopes=_GMAIL_SCOPES, subject=sender_email)
    return build("gmail", "v1", credentials=creds)


def get_gmail_service(sender_email: str):
    """Build a Gmail API client via service-account delegation.

    Synchronous version used by callers that cannot await (e.g. the
    adapters' inbound Gmail processor).  For the async path that also
    supports BYOD tokens, use ``get_gmail_service_async``.
    """
    creds = _service_account_credentials(scopes=_GMAIL_SCOPES, subject=sender_email)
    return build("gmail", "v1", credentials=creds)


# Endpoints - JSON format
@router.post("/create", status_code=201)
async def create_email_user(request: Request):
    data = await request.json()
    local = data.get("local")
    first_name = data.get("first_name")
    last_name = data.get("last_name")
    if not local or not first_name or not last_name:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: local, first_name, last_name",
        )
    primary_email = f"{local}@{SETTINGS.workspace_email_domain}"
    # generate secure password
    password = "".join(
        random.choice(string.ascii_letters + string.digits) for _ in range(32)
    )
    try:
        service = get_admin_service()
        user_body = {
            "name": {"givenName": first_name, "familyName": last_name},
            "primaryEmail": primary_email,
            "password": password,
        }
        res = service.users().insert(body=user_body).execute()
        async with httpx.AsyncClient() as http_client:
            await http_client.post(
                f"{SETTINGS.comms_url}/gmail/watch",
                json={"primary_email": primary_email},
                headers={
                    "Authorization": f"Bearer {SETTINGS.orchestra_admin_key}",
                },
                timeout=30,
            )
        return {"success": True, "user": res}
    except Exception as e:
        logger.error("Failed to create user: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/delete")
async def delete_email_user(request: Request):
    """Delete a Workspace user, treating an already-missing user as success."""
    data = await request.json()
    primary_email = data.get("primary_email")
    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")
    try:
        service = get_admin_service()
        service.users().delete(userKey=primary_email).execute()
        return {
            "success": True,
            "deleted": True,
            "already_absent": False,
            "message": f"User {primary_email} deleted.",
        }
    except Exception as e:
        if _is_google_not_found_error(e):
            logger.info(
                "Workspace user %s already absent during delete",
                primary_email,
            )
            return {
                "success": True,
                "deleted": False,
                "already_absent": True,
                "message": f"User {primary_email} already absent.",
            }
        logger.error("Failed to delete user: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/send")
async def send_email(request: Request):
    data = await request.json()
    sender = data.get("from")
    to = data.get("to")
    cc = data.get("cc")
    bcc = data.get("bcc")
    subject = data.get("subject", "")
    body = data.get("body")
    in_reply_to = data.get("in_reply_to")  # email_id to reply to (threading id)
    attachment = data.get(
        "attachment",
    )  # Optional: {"filename": str, "content_base64": str}

    if not sender or not to or body is None:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: 'from', 'to', 'body'",
        )

    # initialize message
    msg = MIMEMultipart()
    msg["from"] = sender
    msg["to"] = to if isinstance(to, str) else ",".join(to)
    if cc:
        msg["cc"] = cc if isinstance(cc, str) else ",".join(cc)
    if bcc:
        msg["bcc"] = bcc if isinstance(bcc, str) else ",".join(bcc)
    msg["subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    # add attachment if provided
    if attachment:
        try:
            filename = attachment.get("filename", "attachment")
            content_base64 = attachment.get("content_base64", "")
            file_data = base64.b64decode(content_base64)

            part = MIMEBase("application", "octet-stream")
            part.set_payload(file_data)
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"',
            )
            msg.attach(part)
            print(f"Attached file: {filename} ({len(file_data)} bytes)")
        except Exception as e:
            logger.error("Failed to attach file: %s", e)
            raise HTTPException(status_code=400, detail=f"Failed to attach file: {e}")

    # add threading headers if provided
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        # For simple replies, References should be the same as In-Reply-To
        # This is the standard way to handle email threading
        msg["References"] = in_reply_to
    print(f"msg: {msg}")

    raw_msg = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = await get_gmail_service_async(sender)

    sent = service.users().messages().send(userId="me", body={"raw": raw_msg}).execute()
    return {"success": True, "id": sent.get("id")}


@router.post("/watch")
async def watch_email(request: Request):
    data = await request.json()
    user_email = data.get("primary_email")
    if not user_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    gmail_service = await get_gmail_service_async(user_email)

    watch_request = {
        "labelIds": ["INBOX"],
        "topicName": _gmail_topic_path(data.get("topic_name")),
    }
    watch_resp = (
        gmail_service.users()
        .watch(
            userId="me",
            body=watch_request,
        )
        .execute()
    )
    return {"success": True, "historyId": watch_resp.get("historyId")}


@router.delete("/watch")
async def delete_gmail_watch(request: Request):
    """Stop Gmail push notifications for ``primary_email``.

    Mirrors the Outlook/Teams teardown contract used by Orchestra's
    disconnect flow.  Must be called *before* the BYOD access token is
    revoked or cleared — once the token is gone,
    ``get_gmail_service_async`` either sees a revoked token or falls
    through to service-account delegation, which isn't authorized for
    BYOD mailboxes.

    Request body: ``{ "primary_email": "user@domain.com" }``
    """
    data = await request.json()
    user_email = data.get("primary_email")
    if not user_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    gmail_service = await get_gmail_service_async(user_email)
    try:
        gmail_service.users().stop(userId="me").execute()
    except HttpError as exc:
        if _is_google_not_found_error(exc):
            logger.info(
                "Gmail watch already absent for %s during delete",
                user_email,
            )
            return {
                "success": True,
                "primary_email": user_email,
                "already_absent": True,
            }
        logger.error("Failed to stop Gmail watch for %s: %s", user_email, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    return {"success": True, "primary_email": user_email}


@router.get("/attachment")
async def get_attachment(
    receiver_email: str,
    gmail_message_id: str,
    attachment_id: str,
    filename: str | None = None,
):
    try:
        service = await get_gmail_service_async(receiver_email)
        attachment = (
            service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=gmail_message_id, id=attachment_id)
            .execute()
        )
        data = attachment.get("data")
        if not data:
            raise HTTPException(status_code=404, detail="Attachment not found")
        file_bytes = base64.urlsafe_b64decode(data.encode("utf-8"))
        return Response(
            content=file_bytes,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename={filename or 'attachment'}",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
