import os
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Request, Response
from dotenv import load_dotenv

from azure.identity import ClientSecretCredential
from msgraph import GraphServiceClient
from msgraph.generated.users.item.send_mail.send_mail_post_request_body import (
    SendMailPostRequestBody,
)
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.recipient import Recipient
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.subscription import Subscription
from msgraph.generated.models.message import Message

from communication.helpers import ADAPTERS_URL

load_dotenv()

router = APIRouter()

# Azure AD credentials from environment
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")


# Helpers
def get_graph_client() -> GraphServiceClient:
    """
    Create a Microsoft Graph client using client credentials flow.
    Requires Application permissions (not delegated) in Azure AD.
    """
    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        raise HTTPException(
            status_code=500,
            detail="Azure AD credentials not configured. Set AZURE_TENANT_ID, AZURE_CLIENT_ID, and AZURE_CLIENT_SECRET.",
        )

    credential = ClientSecretCredential(
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
    )
    scopes = ["https://graph.microsoft.com/.default"]
    return GraphServiceClient(credentials=credential, scopes=scopes)


# Endpoints
@router.post("/send")
async def send_outlook_email(request: Request):
    """
    Send an email via Microsoft Graph API.

    Request body:
    {
        "from": "sender@yourdomain.com",
        "to": "recipient@example.com" or ["recipient1@example.com", "recipient2@example.com"],
        "cc": "cc@example.com" (optional),
        "bcc": "bcc@example.com" (optional),
        "subject": "Email subject",
        "body": "Email body content",
        "in_reply_to": "conversation_id" (optional, for threading)
    }
    """
    data = await request.json()
    sender = data.get("from")
    to = data.get("to")
    cc = data.get("cc")
    bcc = data.get("bcc")
    subject = data.get("subject", "")
    body = data.get("body")
    in_reply_to = data.get("in_reply_to")  # conversation_id for threading

    if not sender or not to or body is None:
        raise HTTPException(
            status_code=400, detail="Missing required fields: 'from', 'to', 'body'"
        )

    # Normalize recipients to lists
    to_list = [to] if isinstance(to, str) else to
    cc_list = [cc] if isinstance(cc, str) and cc else (cc if cc else [])
    bcc_list = [bcc] if isinstance(bcc, str) and bcc else (bcc if bcc else [])

    try:
        graph_client = get_graph_client()

        # Build message
        message = Message(
            subject=subject,
            body=ItemBody(content=body, content_type=BodyType.Text),
            to_recipients=[
                Recipient(email_address=EmailAddress(address=addr)) for addr in to_list
            ],
        )

        if cc_list:
            message.cc_recipients = [
                Recipient(email_address=EmailAddress(address=addr)) for addr in cc_list
            ]

        if bcc_list:
            message.bcc_recipients = [
                Recipient(email_address=EmailAddress(address=addr)) for addr in bcc_list
            ]

        # For threading/replies - set conversation_id
        if in_reply_to:
            message.conversation_id = in_reply_to

        request_body = SendMailPostRequestBody(
            message=message,
            save_to_sent_items=True,
        )

        await graph_client.users.by_user_id(sender).send_mail.post(request_body)
        print(f"Outlook email sent from {sender} to {to}")
        return {"success": True}

    except Exception as e:
        logging.error("Failed to send Outlook email: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/watch")
async def watch_outlook_email(request: Request):
    """
    Create or renew a subscription (webhook) for new emails in a user's inbox.
    This is idempotent like Gmail's watch - calling it again renews the subscription.
    Microsoft Graph subscriptions expire after max 3 days for mail.

    Request body:
    {
        "primary_email": "user@yourdomain.com",
        "webhook_url": "https://your-domain.com/email/outlook" (optional)
    }
    """
    data = await request.json()
    user_email = data.get("primary_email")
    webhook_url = data.get("webhook_url")

    if not user_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    if not webhook_url:
        # Default to the adapters URL webhook endpoint
        webhook_url = f"{ADAPTERS_URL}/email/outlook"

    try:
        graph_client = get_graph_client()
        expiration = datetime.now(timezone.utc) + timedelta(days=3)
        target_resource = f"users/{user_email}/mailFolders/inbox/messages"

        # Check if subscription already exists
        subscriptions = await graph_client.subscriptions.get()
        existing_subscription = None

        for sub in subscriptions.value:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                existing_subscription = sub
                break

        if existing_subscription:
            # Renew existing subscription
            subscription_update = Subscription(expiration_date_time=expiration)
            result = await graph_client.subscriptions.by_subscription_id(
                existing_subscription.id
            ).patch(subscription_update)

            print(f"Outlook watch renewed for {user_email}")
            return {
                "success": True,
                "action": "renewed",
                "subscription_id": existing_subscription.id,
                "expiration": result.expiration_date_time.isoformat(),
            }
        else:
            # Create new subscription
            subscription = Subscription(
                change_type="created",
                notification_url=webhook_url,
                resource=target_resource,
                expiration_date_time=expiration,
                client_state=os.getenv(
                    "OUTLOOK_WEBHOOK_SECRET", "unify-outlook-webhook"
                ),
            )

            result = await graph_client.subscriptions.post(subscription)
            print(
                f"Outlook watch created for {user_email}, subscription_id: {result.id}"
            )

            return {
                "success": True,
                "action": "created",
                "subscription_id": result.id,
                "expiration": result.expiration_date_time.isoformat(),
            }

    except Exception as e:
        logging.error("Failed to create/renew Outlook watch: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/watch")
async def delete_outlook_watch(request: Request):
    """
    Delete a subscription.

    Request body:
    {
        "primary_email": "user@yourdomain.com"
    }
    """
    data = await request.json()
    primary_email = data.get("primary_email")

    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    try:
        graph_client = get_graph_client()

        # Find the subscription ID for this email
        subscriptions = await graph_client.subscriptions.get()

        target_resource = f"users/{primary_email}/mailFolders/inbox/messages"
        found_subscription = None

        for sub in subscriptions.value:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                found_subscription = sub
                break

        if not found_subscription:
            raise HTTPException(
                status_code=404,
                detail=f"No subscription found for email: {primary_email}",
            )

        subscription_id = found_subscription.id
        print(f"Found subscription {subscription_id} for {primary_email}")

        await graph_client.subscriptions.by_subscription_id(subscription_id).delete()

        print(f"Outlook watch deleted for {primary_email}")
        return {
            "success": True,
            "primary_email": primary_email,
            "message": f"Subscription for {primary_email} deleted.",
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error("Failed to delete Outlook watch: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/attachment")
async def get_outlook_attachment(
    user_email: str,
    message_id: str,
    attachment_id: str,
    filename: str | None = None,
):
    """
    Get an attachment from a message.
    """
    try:
        graph_client = get_graph_client()

        attachment = (
            await graph_client.users.by_user_id(user_email)
            .messages.by_message_id(message_id)
            .attachments.by_attachment_id(attachment_id)
            .get()
        )

        if not attachment:
            raise HTTPException(status_code=404, detail="Attachment not found")

        # For file attachments, get the content
        content_bytes = getattr(attachment, "content_bytes", None)
        if not content_bytes:
            raise HTTPException(status_code=404, detail="Attachment content not found")

        return Response(
            content=content_bytes,
            media_type=attachment.content_type or "application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename={filename or attachment.name or 'attachment'}"
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.error("Failed to get Outlook attachment: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
