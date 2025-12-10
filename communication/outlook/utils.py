"""
Outlook helpers and additional endpoints.
These are temporary/WIP functions that will be moved to adapters later.
"""

import os
import logging
from fastapi import APIRouter, HTTPException
from msgraph import GraphServiceClient
from msgraph.generated.models.message import Message
from azure.identity import ClientSecretCredential

router = APIRouter()

# Azure AD credentials from environment
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")


def get_graph_client() -> GraphServiceClient:
    """
    Create a Microsoft Graph client using client credentials flow.
    """
    if not all([AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET]):
        raise HTTPException(
            status_code=500,
            detail="Azure AD credentials not configured.",
        )

    credential = ClientSecretCredential(
        tenant_id=AZURE_TENANT_ID,
        client_id=AZURE_CLIENT_ID,
        client_secret=AZURE_CLIENT_SECRET,
    )
    return GraphServiceClient(
        credentials=credential,
        scopes=["https://graph.microsoft.com/.default"],
    )


async def process_outlook_notification(user_email: str, message_id: str):
    """
    Process a notification for a new email.
    Fetches the message details and publishes to Pub/Sub.
    """
    try:
        graph_client = get_graph_client()

        # Fetch the message
        message = (
            await graph_client.users.by_user_id(user_email)
            .messages.by_message_id(message_id)
            .get()
        )

        if not message:
            print(f"Message {message_id} not found")
            return

        # Extract message details
        message_data = {
            "id": message.id,
            "conversation_id": message.conversation_id,
            "subject": message.subject,
            "body": message.body.content if message.body else "",
            "sender": message.from_.email_address.address if message.from_ else "",
            "to": [r.email_address.address for r in (message.to_recipients or [])],
            "cc": [r.email_address.address for r in (message.cc_recipients or [])],
            "bcc": [r.email_address.address for r in (message.bcc_recipients or [])],
            "received_at": (
                message.received_date_time.isoformat()
                if message.received_date_time
                else None
            ),
            "has_attachments": message.has_attachments,
        }

        print(f"Processed Outlook message: {message_data}")

        # Mark as read
        await graph_client.users.by_user_id(user_email).messages.by_message_id(
            message_id
        ).patch(Message(is_read=True))

        # TODO: Publish to your Pub/Sub pipeline similar to Gmail
        # This would integrate with your existing adapters/main.py flow
        # publish_outlook_thread(assistant_id, message_data, contacts)

        return message_data

    except Exception as e:
        logging.error("Error processing Outlook notification: %s", e)
        raise


@router.get("/message")
async def get_outlook_message(
    user_email: str,
    message_id: str,
):
    """
    Get a specific message by ID.
    """
    try:
        graph_client = get_graph_client()

        message = (
            await graph_client.users.by_user_id(user_email)
            .messages.by_message_id(message_id)
            .get()
        )

        if not message:
            raise HTTPException(status_code=404, detail="Message not found")

        return {
            "id": message.id,
            "conversation_id": message.conversation_id,
            "subject": message.subject,
            "body": message.body.content if message.body else "",
            "body_type": message.body.content_type.value if message.body else None,
            "sender": message.from_.email_address.address if message.from_ else "",
            "to": [r.email_address.address for r in (message.to_recipients or [])],
            "cc": [r.email_address.address for r in (message.cc_recipients or [])],
            "received_at": (
                message.received_date_time.isoformat()
                if message.received_date_time
                else None
            ),
            "has_attachments": message.has_attachments,
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error("Failed to get Outlook message: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/thread")
async def get_outlook_thread(
    user_email: str,
    conversation_id: str,
):
    """
    Get all messages in a conversation thread.
    """
    try:
        graph_client = get_graph_client()

        # Filter messages by conversation ID
        messages = await graph_client.users.by_user_id(user_email).messages.get(
            query_params={
                "filter": f"conversationId eq '{conversation_id}'",
                "orderby": "receivedDateTime asc",
            }
        )

        if not messages or not messages.value:
            raise HTTPException(status_code=404, detail="Thread not found")

        thread_messages = []
        for msg in messages.value:
            thread_messages.append(
                {
                    "id": msg.id,
                    "subject": msg.subject,
                    "body": msg.body.content if msg.body else "",
                    "sender": msg.from_.email_address.address if msg.from_ else "",
                    "to": [r.email_address.address for r in (msg.to_recipients or [])],
                    "cc": [r.email_address.address for r in (msg.cc_recipients or [])],
                    "received_at": (
                        msg.received_date_time.isoformat()
                        if msg.received_date_time
                        else None
                    ),
                }
            )

        return {
            "conversation_id": conversation_id,
            "messages": thread_messages,
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error("Failed to get Outlook thread: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
