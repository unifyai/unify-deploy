import os
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Request, Response
import httpx

from msgraph import GraphServiceClient
from msgraph.generated.users.item.send_mail.send_mail_post_request_body import (
    SendMailPostRequestBody,
)
from msgraph.generated.users.item.messages.item.reply.reply_post_request_body import (
    ReplyPostRequestBody,
)
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.recipient import Recipient
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.subscription import Subscription
from msgraph.generated.models.message import Message
from azure.core.credentials import AccessToken, TokenCredential

from communication.helpers import ADAPTERS_URL, ORCHESTRA_URL

router = APIRouter()


class TokenCredentialFromSecret(TokenCredential):
    """Wraps a stored access token for use with Microsoft Graph SDK."""

    def __init__(self, access_token: str):
        self._token = access_token

    def get_token(self, *scopes, **kwargs) -> AccessToken:
        # Expiry doesn't matter - scheduled job keeps token fresh
        return AccessToken(
            self._token, int(datetime.now(tz=timezone.utc).timestamp()) + 3600
        )


async def get_graph_client(user_email: str) -> GraphServiceClient:
    """Get Graph client using stored access token for the given assistant email."""
    admin_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    if not admin_key:
        raise HTTPException(
            status_code=500, detail="ORCHESTRA_ADMIN_KEY not configured"
        )

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{ORCHESTRA_URL}/admin/assistant",
            params={"email": user_email},
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=30.0,
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=404, detail=f"Assistant not found: {user_email}"
        )

    assistants = response.json().get("info", [])
    if not assistants:
        raise HTTPException(
            status_code=404, detail=f"Assistant not found: {user_email}"
        )

    secrets = assistants[0].get("secrets", {})
    access_token = secrets.get("MICROSOFT_ACCESS_TOKEN")
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail=f"No Microsoft access token for {user_email}. Complete OAuth first.",
        )

    return GraphServiceClient(
        credentials=TokenCredentialFromSecret(access_token),
        scopes=["https://graph.microsoft.com/.default"],
    )


@router.post("/send")
async def send_outlook_email(request: Request):
    """
    Send an email via Microsoft Graph API.

    Request body:
    {
        "from": "sender@yourdomain.com",
        "to": "recipient@example.com" or ["r1@example.com", "r2@example.com"],
        "cc": "cc@example.com" (optional),
        "bcc": "bcc@example.com" (optional),
        "subject": "Email subject",
        "body": "Email body content",
        "in_reply_to": "message_id" (optional, for replies)
    }
    """
    data = await request.json()
    sender = data.get("from")
    to = data.get("to")
    subject = data.get("subject", "")
    body = data.get("body")
    in_reply_to = data.get("in_reply_to")

    if not sender or not to or body is None:
        raise HTTPException(
            status_code=400, detail="Missing required fields: from, to, body"
        )

    to_list = [to] if isinstance(to, str) else to
    cc = data.get("cc")
    bcc = data.get("bcc")
    cc_list = [cc] if isinstance(cc, str) and cc else (cc or [])
    bcc_list = [bcc] if isinstance(bcc, str) and bcc else (bcc or [])

    try:
        graph = await get_graph_client(sender)

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

        # Use /me endpoint for delegated permissions (token is for this user)
        if in_reply_to:
            await graph.me.messages.by_message_id(in_reply_to).reply.post(
                ReplyPostRequestBody(message=message)
            )
        else:
            request_body = SendMailPostRequestBody(
                message=message, save_to_sent_items=True
            )
            await graph.me.send_mail.post(request_body)

        logging.info(f"Outlook email sent from {sender} to {to}")
        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to send Outlook email: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/watch")
async def watch_outlook_email(request: Request):
    """
    Create webhook subscription for new emails in inbox.
    Microsoft Graph subscriptions expire after 3 days max.

    Request body:
    {
        "primary_email": "user@yourdomain.com",
        "webhook_url": "https://..." (optional, defaults to adapters URL)
    }
    """
    data = await request.json()
    user_email = data.get("primary_email")
    webhook_url = data.get("webhook_url") or f"{ADAPTERS_URL}/microsoft/router"

    if not user_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    try:
        graph = await get_graph_client(user_email)
        target_resource = f"users/{user_email}/mailFolders/inbox/messages"

        # Delete existing subscriptions for this resource
        subs = await graph.subscriptions.get()
        for sub in subs.value or []:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                try:
                    await graph.subscriptions.by_subscription_id(sub.id).delete()
                except Exception:
                    pass

        # Create new subscription
        result = await graph.subscriptions.post(
            Subscription(
                change_type="created",
                notification_url=webhook_url,
                resource=target_resource,
                expiration_date_time=datetime.now(timezone.utc) + timedelta(days=3),
                client_state=os.getenv(
                    "OUTLOOK_WEBHOOK_SECRET", "unify-outlook-webhook"
                ),
            )
        )

        logging.info(f"Outlook watch created for {user_email}: {result.id}")
        return {
            "success": True,
            "subscription_id": result.id,
            "expiration": result.expiration_date_time.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to create Outlook watch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/watch")
async def delete_outlook_watch(request: Request):
    """
    Delete email subscription.

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
                logging.info(f"Outlook watch deleted for {primary_email}")
                return {"success": True, "primary_email": primary_email}

        raise HTTPException(
            status_code=404, detail=f"No subscription found for {primary_email}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to delete Outlook watch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/attachment")
async def get_outlook_attachment(
    user_email: str,
    message_id: str,
    attachment_id: str,
    filename: str | None = None,
):
    """Get an attachment from a message."""
    try:
        graph = await get_graph_client(user_email)
        attachment = (
            await graph.me.messages.by_message_id(message_id)
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
                "Content-Disposition": f"attachment; filename={filename or attachment.name or 'attachment'}"
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to get Outlook attachment: {e}")
        raise HTTPException(status_code=500, detail=str(e))
