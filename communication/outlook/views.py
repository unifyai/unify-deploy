import base64
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.file_attachment import FileAttachment
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.message import Message
from msgraph.generated.models.recipient import Recipient
from msgraph.generated.models.subscription import Subscription
from msgraph.generated.users.item.messages.item.create_reply.create_reply_post_request_body import (
    CreateReplyPostRequestBody,
)
from msgraph.generated.users.item.messages.item.reply.reply_post_request_body import (
    ReplyPostRequestBody,
)
from msgraph.generated.users.item.send_mail.send_mail_post_request_body import (
    SendMailPostRequestBody,
)

from communication.helpers import (
    _lookup_assistant,
    get_admin_graph_client,
    get_graph_client,
    graph_client_from_assistant,
)
from common.settings import SETTINGS

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_RETRIES = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_user_node(graph, sender: str, assistant: dict):
    """Return the Graph request builder targeting the correct user.

    Per-user OAuth tokens use ``/me``.  Admin app credentials (provisioned
    accounts) must address ``/users/{email}`` because the token isn't scoped
    to a single user.
    """
    has_user_token = bool(assistant.get("secrets", {}).get("MICROSOFT_ACCESS_TOKEN"))
    if has_user_token:
        return graph.me
    return graph.users.by_user_id(sender)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


# NOTE: ``POST /outlook/create`` and ``POST /outlook/backfill-tokens`` were
# retired together with the wider platform ``@unify.ai`` mailbox feature.
# Email contacts are now BYOD-only (delegated user OAuth via the
# ``microsoft/auth/callback`` flow in ``adapters``); the only directory
# write Communication still performs is the explicit ``DELETE
# /outlook/delete`` invoked by Orchestra's teardown worker for straggler
# cleanup.


@router.delete("/delete")
async def delete_outlook_user(request: Request):
    """Delete an MS365 user.  Treats already-absent users as success.

    Request body: { "primary_email": "alice@unify.ai" }
    """
    data = await request.json()
    primary_email = data.get("primary_email")
    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    graph = get_admin_graph_client()
    try:
        await graph.users.by_user_id(primary_email).delete()
        logger.info("Deleted MS365 user %s", primary_email)
        return {
            "success": True,
            "deleted": True,
            "already_absent": False,
            "message": f"User {primary_email} deleted.",
        }
    except Exception as e:
        if "does not exist" in str(e).lower() or "not found" in str(e).lower():
            logger.info("MS365 user %s already absent during delete", primary_email)
            return {
                "success": True,
                "deleted": False,
                "already_absent": True,
                "message": f"User {primary_email} already absent.",
            }
        logger.error("Failed to delete MS365 user %s: %s", primary_email, e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


@router.post("/send")
async def send_outlook_email(request: Request):
    """Send an email via Microsoft Graph API.

    Request body:
    {
        "from": "sender@yourdomain.com",
        "to": "recipient@example.com" or ["r1@example.com", "r2@example.com"],
        "cc": "cc@example.com" (optional),
        "bcc": "bcc@example.com" (optional),
        "subject": "Email subject",
        "body": "Email body content",
        "in_reply_to": "message_id" (optional, for replies),
        "attachment": {"filename": str, "content_base64": str} (optional)
    }
    """
    data = await request.json()
    sender = data.get("from")
    to = data.get("to")
    subject = data.get("subject", "")
    body = data.get("body")
    in_reply_to = data.get("in_reply_to")
    attachment = data.get("attachment")

    if not sender or not to or body is None:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: from, to, body",
        )

    to_list = [to] if isinstance(to, str) else to
    cc = data.get("cc")
    bcc = data.get("bcc")
    cc_list = [cc] if isinstance(cc, str) and cc else (cc or [])
    bcc_list = [bcc] if isinstance(bcc, str) and bcc else (bcc or [])

    try:
        assistant = await _lookup_assistant(sender)
        graph = graph_client_from_assistant(assistant, sender)
        user = await _get_user_node(graph, sender, assistant)

        message = Message(
            subject=subject,
            body=ItemBody(content=body, content_type=BodyType.Text),
            to_recipients=[
                Recipient(email_address=EmailAddress(address=a)) for a in to_list
            ],
        )
        if cc_list:
            message.cc_recipients = [
                Recipient(email_address=EmailAddress(address=a)) for a in cc_list
            ]
        if bcc_list:
            message.bcc_recipients = [
                Recipient(email_address=EmailAddress(address=a)) for a in bcc_list
            ]

        file_attachment = None
        if attachment:
            file_attachment = FileAttachment(
                odata_type="#microsoft.graph.fileAttachment",
                name=attachment.get("filename", "attachment"),
                content_type="application/octet-stream",
                content_bytes=base64.b64decode(
                    attachment.get("content_base64", ""),
                ),
            )

        if in_reply_to:
            if file_attachment:
                draft = await user.messages.by_message_id(
                    in_reply_to,
                ).create_reply.post(
                    CreateReplyPostRequestBody(message=message),
                )
                await user.messages.by_message_id(
                    draft.id,
                ).attachments.post(file_attachment)
                await user.messages.by_message_id(draft.id).send.post()
            else:
                await user.messages.by_message_id(in_reply_to).reply.post(
                    ReplyPostRequestBody(message=message),
                )
        else:
            if file_attachment:
                message.attachments = [file_attachment]
            await user.send_mail.post(
                SendMailPostRequestBody(
                    message=message,
                    save_to_sent_items=True,
                ),
            )

        logger.info("Outlook email sent from %s to %s", sender, to)
        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to send Outlook email: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Watch
# ---------------------------------------------------------------------------


@router.post("/watch")
async def watch_outlook_email(request: Request):
    """Create webhook subscription for new emails in inbox.

    Microsoft Graph subscriptions expire after 3 days max.

    Request body:
    {
        "primary_email": "user@yourdomain.com",
        "webhook_url": "https://..." (optional, defaults to adapters URL)
    }
    """
    data = await request.json()
    user_email = data.get("primary_email")
    webhook_url = data.get("webhook_url") or f"{SETTINGS.adapters_url}/microsoft/router"

    if not user_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    try:
        graph = await get_graph_client(user_email)
        target_resource = f"users/{user_email}/mailFolders/inbox/messages"

        subs = await graph.subscriptions.get()
        for sub in subs.value or []:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                try:
                    await graph.subscriptions.by_subscription_id(sub.id).delete()
                except Exception:
                    pass

        webhook_secret = os.getenv("OUTLOOK_WEBHOOK_SECRET", "unify-outlook-webhook")
        client_state = f"{webhook_secret}::{user_email}"

        for attempt in range(MAX_RETRIES + 1):
            try:
                result = await graph.subscriptions.post(
                    Subscription(
                        change_type="created",
                        notification_url=webhook_url,
                        resource=target_resource,
                        expiration_date_time=datetime.now(timezone.utc)
                        + timedelta(days=3),
                        client_state=client_state,
                    ),
                )
                logger.info("Outlook watch created for %s: %s", user_email, result.id)
                return {
                    "success": True,
                    "subscription_id": result.id,
                    "expiration": result.expiration_date_time.isoformat(),
                }
            except Exception as e:
                error_str = str(e).lower()
                is_validation_timeout = (
                    "validation" in error_str and "timeout" in error_str
                )
                if is_validation_timeout and attempt < MAX_RETRIES:
                    logger.warning(
                        "Outlook watch validation timeout for %s, retrying...",
                        user_email,
                    )
                    continue
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to create Outlook watch: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/watch")
async def delete_outlook_watch(request: Request):
    """Delete email subscription.

    Request body: { "primary_email": "user@yourdomain.com" }
    """
    data = await request.json()
    primary_email = data.get("primary_email")

    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    try:
        graph = await get_graph_client(primary_email)
        target_resource = f"users/{primary_email}/mailFolders/inbox/messages"

        subs = await graph.subscriptions.get()
        for sub in subs.value or []:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                await graph.subscriptions.by_subscription_id(sub.id).delete()
                logger.info("Outlook watch deleted for %s", primary_email)
                return {"success": True, "primary_email": primary_email}

        raise HTTPException(
            status_code=404,
            detail=f"No subscription found for {primary_email}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete Outlook watch: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Attachment
# ---------------------------------------------------------------------------


@router.get("/attachment")
async def get_outlook_attachment(
    user_email: str,
    message_id: str,
    attachment_id: str,
    filename: str | None = None,
):
    """Get an attachment from a message."""
    try:
        assistant = await _lookup_assistant(user_email)
        graph = graph_client_from_assistant(assistant, user_email)
        user = await _get_user_node(graph, user_email, assistant)

        attachment = (
            await user.messages.by_message_id(message_id)
            .attachments.by_attachment_id(attachment_id)
            .get()
        )

        if not attachment:
            raise HTTPException(status_code=404, detail="Attachment not found")

        content = getattr(attachment, "content_bytes", None)
        if not content:
            raise HTTPException(status_code=404, detail="Attachment content not found")

        return Response(
            content=content,
            media_type=attachment.content_type or "application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename={filename or attachment.name or 'attachment'}",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get Outlook attachment: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
