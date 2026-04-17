import base64
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.chat_message import ChatMessage
from msgraph.generated.models.chat_message_attachment import ChatMessageAttachment
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.subscription import Subscription

from communication.helpers import get_graph_client, get_ms_graph_client
from common.settings import SETTINGS

router = APIRouter()

# Retry configuration for subscription creation (1 retry = 2 total attempts)
MAX_RETRIES = 1


def _chat_watch_resource(user_email: str, has_user_token: bool) -> str:
    """Resource path for a Teams chat subscription.

    Delegated tokens use ``/me``; app-only credentials have no ``/me`` and
    must scope explicitly to ``/users/{email}``.
    """
    if has_user_token:
        return "/me/chats/getAllMessages"
    return f"/users/{user_email}/chats/getAllMessages"


async def _upload_and_build_attachments(
    graph,
    raw_attachments: list[dict],
) -> list[ChatMessageAttachment]:
    """Upload files to OneDrive and return Graph ChatMessageAttachment objects.

    Each item in *raw_attachments* should have ``filename`` and
    ``content_base64``.  Files are written to ``Teams Attachments/`` in the
    sender's personal OneDrive so the Graph API can reference them.
    """
    result: list[ChatMessageAttachment] = []
    for att in raw_attachments:
        filename = att.get("filename", "attachment")
        content_b64 = att.get("content_base64", "")
        if not content_b64:
            continue
        file_bytes = base64.b64decode(content_b64)

        safe_name = f"{uuid.uuid4().hex[:8]}_{filename}"
        drive_item = await graph.me.drive.root.item_with_path(
            f"Teams Attachments/{safe_name}"
        ).content.put(file_bytes)

        att_id = uuid.uuid4().hex
        result.append(
            ChatMessageAttachment(
                id=att_id,
                content_type="reference",
                content_url=drive_item.web_url,
                name=filename,
            ),
        )
    return result


def _build_chat_message(
    body: str,
    content_type: str,
    attachments: list[ChatMessageAttachment],
) -> ChatMessage:
    """Build a ChatMessage, embedding ``<attachment>`` tags when needed."""
    if attachments:
        att_tags = "".join(
            f'<attachment id="{a.id}"></attachment>' for a in attachments
        )
        html_body = f"{body} {att_tags}" if body else att_tags
        return ChatMessage(
            body=ItemBody(content=html_body, content_type=BodyType.Html),
            attachments=attachments,
        )
    return ChatMessage(
        body=ItemBody(
            content=body,
            content_type=BodyType.Html if content_type == "html" else BodyType.Text,
        ),
    )


@router.post("/send")
async def send_teams_chat(request: Request):
    """
    Send a message to a Teams chat.

    Request body:
    {
        "from": "sender@yourdomain.com",
        "chat_id": "19:meeting_xxx@thread.v2",
        "body": "Message content",
        "content_type": "text" or "html" (optional, defaults to "text"),
        "attachments": [{"filename": str, "content_base64": str}] (optional)
    }
    """
    data = await request.json()
    sender = data.get("from")
    chat_id = data.get("chat_id")
    body = data.get("body")
    content_type = data.get("content_type", "text")
    raw_attachments = data.get("attachments") or []

    if not sender or not chat_id or body is None:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: from, chat_id, body",
        )

    try:
        graph = await get_graph_client(sender)

        attachments = await _upload_and_build_attachments(graph, raw_attachments)
        message = _build_chat_message(body, content_type, attachments)

        result = await graph.me.chats.by_chat_id(chat_id).messages.post(message)

        logging.info(f"Teams chat message sent from {sender} to chat {chat_id}")
        return {"success": True, "message_id": result.id}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to send Teams chat message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chats")
async def list_teams_chats(user_email: str):
    """
    List all chats for a user.

    Query params:
    - user_email: The assistant email address
    """
    try:
        graph = await get_graph_client(user_email)
        chats = await graph.me.chats.get()

        chat_list = []
        for chat in chats.value or []:
            chat_list.append(
                {
                    "id": chat.id,
                    "topic": chat.topic,
                    "chat_type": str(chat.chat_type) if chat.chat_type else None,
                    "created_datetime": (
                        chat.created_date_time.isoformat()
                        if chat.created_date_time
                        else None
                    ),
                },
            )

        return {"success": True, "chats": chat_list}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to list Teams chats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/watch")
async def watch_teams_chat(request: Request):
    """
    Create webhook subscription for new chat messages.
    Teams chat subscriptions expire after 60 minutes max.

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
        graph, has_user_token = await get_ms_graph_client(user_email)
        target_resource = _chat_watch_resource(user_email, has_user_token)

        # Delete existing subscriptions for this resource
        subs = await graph.subscriptions.get()
        for sub in subs.value or []:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                try:
                    await graph.subscriptions.by_subscription_id(sub.id).delete()
                except Exception:
                    pass

        # Create new subscription with retry logic for validation timeouts
        # Encode assistant email in clientState so we can identify them in notifications
        # Format: {secret}::{email}
        webhook_secret = os.getenv("TEAMS_WEBHOOK_SECRET", "unify-teams-webhook")
        client_state = f"{webhook_secret}::{user_email}"

        for attempt in range(MAX_RETRIES + 1):
            try:
                result = await graph.subscriptions.post(
                    Subscription(
                        change_type="created",
                        notification_url=webhook_url,
                        resource=target_resource,
                        expiration_date_time=datetime.now(timezone.utc)
                        + timedelta(minutes=60),
                        client_state=client_state,
                    ),
                )
                logging.info(f"Teams chat watch created for {user_email}: {result.id}")
                return {
                    "success": True,
                    "action": "created",
                    "subscription_id": result.id,
                    "expiration": result.expiration_date_time.isoformat(),
                    "mode": "delegated" if has_user_token else "app_only",
                }
            except Exception as e:
                error_str = str(e).lower()
                is_validation_timeout = (
                    "validation" in error_str and "timeout" in error_str
                )
                if is_validation_timeout and attempt < MAX_RETRIES:
                    logging.warning(
                        f"Teams chat watch validation timeout for {user_email}, retrying...",
                    )
                    continue
                raise

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to create Teams chat watch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/watch")
async def delete_teams_watch(request: Request):
    """
    Delete Teams chat subscription.

    Request body: { "primary_email": "user@yourdomain.com" }
    """
    data = await request.json()
    primary_email = data.get("primary_email")

    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    try:
        graph, has_user_token = await get_ms_graph_client(primary_email)
        target_resource = _chat_watch_resource(primary_email, has_user_token)

        subs = await graph.subscriptions.get()
        for sub in subs.value or []:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                await graph.subscriptions.by_subscription_id(sub.id).delete()
                logging.info(f"Teams chat watch deleted for {primary_email}")
                return {"success": True, "primary_email": primary_email}

        raise HTTPException(
            status_code=404,
            detail=f"No subscription found for {primary_email}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to delete Teams chat watch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/subscriptions")
async def list_teams_subscriptions(primary_email: str):
    """List Graph subscriptions visible to the assistant's Graph client.

    Works for both delegated (BYOD) and app-only (us-provisioned)
    credentials — the auth selection is centralized in
    ``get_ms_graph_client``.  The scheduler uses this to discover
    existing channel subscriptions without needing a per-user OAuth
    token of its own.
    """
    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    try:
        graph, has_user_token = await get_ms_graph_client(primary_email)
        subs = await graph.subscriptions.get()

        out = []
        for sub in subs.value or []:
            out.append(
                {
                    "id": sub.id,
                    "resource": sub.resource,
                    "expiration_date_time": (
                        sub.expiration_date_time.isoformat()
                        if sub.expiration_date_time
                        else None
                    ),
                    "client_state": sub.client_state,
                    "change_type": sub.change_type,
                },
            )

        return {
            "success": True,
            "primary_email": primary_email,
            "mode": "delegated" if has_user_token else "app_only",
            "subscriptions": out,
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to list Teams subscriptions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/messages/{chat_id}")
async def get_teams_chat_messages(
    chat_id: str,
    user_email: str,
    top: int = 50,
):
    """
    Get messages from a specific Teams chat.

    Path params:
    - chat_id: The chat ID

    Query params:
    - user_email: The assistant email address
    - top: Number of messages to retrieve (default 50)
    """
    try:
        graph = await get_graph_client(user_email)

        messages = await graph.me.chats.by_chat_id(chat_id).messages.get()

        message_list = []
        for msg in (messages.value or [])[:top]:
            sender_info = msg.from_
            sender_name = "Unknown"
            sender_id = None

            if sender_info and sender_info.user:
                sender_name = sender_info.user.display_name or "Unknown"
                sender_id = sender_info.user.id

            message_list.append(
                {
                    "id": msg.id,
                    "sender": sender_name,
                    "sender_id": sender_id,
                    "content": msg.body.content if msg.body else None,
                    "content_type": (
                        str(msg.body.content_type)
                        if msg.body and msg.body.content_type
                        else None
                    ),
                    "created_datetime": (
                        msg.created_date_time.isoformat()
                        if msg.created_date_time
                        else None
                    ),
                },
            )

        return {"success": True, "messages": message_list}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to get Teams chat messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Teams Channel Endpoints (for team channels, not 1:1 chats)
# =============================================================================


@router.get("/teams")
async def list_joined_teams(user_email: str):
    """
    List all teams the user is a member of.

    Query params:
    - user_email: The assistant email address
    """
    try:
        graph = await get_graph_client(user_email)
        teams = await graph.me.joined_teams.get()

        team_list = []
        for team in teams.value or []:
            team_list.append(
                {
                    "id": team.id,
                    "display_name": team.display_name,
                    "description": team.description,
                },
            )

        return {"success": True, "teams": team_list}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to list joined teams: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/teams/{team_id}/channels")
async def list_team_channels(team_id: str, user_email: str):
    """
    List all channels in a team.

    Path params:
    - team_id: The team ID

    Query params:
    - user_email: The assistant email address
    """
    try:
        graph = await get_graph_client(user_email)
        channels = await graph.teams.by_team_id(team_id).channels.get()

        channel_list = []
        for channel in channels.value or []:
            channel_list.append(
                {
                    "id": channel.id,
                    "display_name": channel.display_name,
                    "description": channel.description,
                    "membership_type": (
                        str(channel.membership_type)
                        if channel.membership_type
                        else None
                    ),
                },
            )

        return {"success": True, "channels": channel_list}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to list team channels: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/watch-channel")
async def watch_teams_channel(request: Request):
    """
    Create webhook subscription for new messages in a Teams channel.
    Channel subscriptions expire after 60 minutes max.

    Request body:
    {
        "primary_email": "user@yourdomain.com",
        "team_id": "team-uuid",
        "channel_id": "19:channel-id@thread.tacv2",
        "webhook_url": "https://..." (optional, defaults to adapters URL)
    }
    """
    data = await request.json()
    user_email = data.get("primary_email")
    team_id = data.get("team_id")
    channel_id = data.get("channel_id")
    webhook_url = data.get("webhook_url") or f"{SETTINGS.adapters_url}/microsoft/router"

    if not user_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")
    if not team_id:
        raise HTTPException(status_code=400, detail="Missing team_id")
    if not channel_id:
        raise HTTPException(status_code=400, detail="Missing channel_id")

    try:
        graph, has_user_token = await get_ms_graph_client(user_email)

        # Channel messages resource path is identical for delegated and
        # app-only permissions — no /me/ in the path.
        target_resource = f"/teams/{team_id}/channels/{channel_id}/messages"

        # Delete existing subscriptions for this exact resource
        subs = await graph.subscriptions.get()
        for sub in subs.value or []:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                try:
                    await graph.subscriptions.by_subscription_id(sub.id).delete()
                except Exception:
                    pass

        # Create new subscription with retry logic for validation timeouts
        # Format: {secret}::{email} - same as chat watch
        # team_id and channel_id are extracted from the resource path in the adapter
        webhook_secret = os.getenv("TEAMS_WEBHOOK_SECRET", "unify-teams-webhook")
        client_state = f"{webhook_secret}::{user_email}"

        for attempt in range(MAX_RETRIES + 1):
            try:
                result = await graph.subscriptions.post(
                    Subscription(
                        change_type="created",
                        notification_url=webhook_url,
                        resource=target_resource,
                        expiration_date_time=datetime.now(timezone.utc)
                        + timedelta(minutes=60),
                        client_state=client_state,
                    ),
                )
                logging.info(
                    f"Teams channel watch created for {user_email} on {team_id}/{channel_id}: {result.id}",
                )
                return {
                    "success": True,
                    "action": "created",
                    "subscription_id": result.id,
                    "team_id": team_id,
                    "channel_id": channel_id,
                    "expiration": result.expiration_date_time.isoformat(),
                    "mode": "delegated" if has_user_token else "app_only",
                }
            except Exception as e:
                error_str = str(e).lower()
                is_validation_timeout = (
                    "validation" in error_str and "timeout" in error_str
                )
                if is_validation_timeout and attempt < MAX_RETRIES:
                    logging.warning(
                        f"Teams channel watch validation timeout for {user_email} "
                        f"on {team_id}/{channel_id}, retrying...",
                    )
                    continue
                raise

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to create Teams channel watch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/watch-channel")
async def delete_teams_channel_watch(request: Request):
    """
    Delete Teams channel subscription.

    Request body:
    {
        "primary_email": "user@yourdomain.com",
        "team_id": "team-uuid",
        "channel_id": "19:channel-id@thread.tacv2"
    }
    """
    data = await request.json()
    primary_email = data.get("primary_email")
    team_id = data.get("team_id")
    channel_id = data.get("channel_id")

    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")
    if not team_id:
        raise HTTPException(status_code=400, detail="Missing team_id")
    if not channel_id:
        raise HTTPException(status_code=400, detail="Missing channel_id")

    try:
        graph = await get_graph_client(primary_email)
        target_resource = f"/teams/{team_id}/channels/{channel_id}/messages"

        subs = await graph.subscriptions.get()
        for sub in subs.value or []:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                await graph.subscriptions.by_subscription_id(sub.id).delete()
                logging.info(
                    f"Teams channel watch deleted for {primary_email} on {team_id}/{channel_id}",
                )
                return {
                    "success": True,
                    "primary_email": primary_email,
                    "team_id": team_id,
                    "channel_id": channel_id,
                }

        raise HTTPException(
            status_code=404,
            detail=f"No subscription found for channel {channel_id} in team {team_id}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to delete Teams channel watch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/channel/{team_id}/{channel_id}/send")
async def send_teams_channel_message(
    team_id: str,
    channel_id: str,
    request: Request,
):
    """
    Send a message to a Teams channel.

    Path params:
    - team_id: The team ID
    - channel_id: The channel ID

    Request body:
    {
        "from": "sender@yourdomain.com",
        "body": "Message content",
        "content_type": "text" or "html" (optional, defaults to "text"),
        "attachments": [{"filename": str, "content_base64": str}] (optional)
    }
    """
    data = await request.json()
    sender = data.get("from")
    body = data.get("body")
    content_type = data.get("content_type", "text")
    raw_attachments = data.get("attachments") or []

    if not sender or body is None:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: from, body",
        )

    try:
        graph = await get_graph_client(sender)

        attachments = await _upload_and_build_attachments(graph, raw_attachments)
        message = _build_chat_message(body, content_type, attachments)

        result = (
            await graph.teams.by_team_id(team_id)
            .channels.by_channel_id(channel_id)
            .messages.post(message)
        )

        logging.info(
            f"Teams channel message sent from {sender} to {team_id}/{channel_id}",
        )
        return {"success": True, "message_id": result.id}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to send Teams channel message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/channel/{team_id}/{channel_id}/messages")
async def get_teams_channel_messages(
    team_id: str,
    channel_id: str,
    user_email: str,
    top: int = 50,
):
    """
    Get messages from a Teams channel.

    Path params:
    - team_id: The team ID
    - channel_id: The channel ID

    Query params:
    - user_email: The assistant email address
    - top: Number of messages to retrieve (default 50)
    """
    try:
        graph = await get_graph_client(user_email)

        messages = (
            await graph.teams.by_team_id(team_id)
            .channels.by_channel_id(channel_id)
            .messages.get()
        )

        message_list = []
        for msg in (messages.value or [])[:top]:
            sender_info = msg.from_
            sender_name = "Unknown"
            sender_id = None

            if sender_info and sender_info.user:
                sender_name = sender_info.user.display_name or "Unknown"
                sender_id = sender_info.user.id

            message_list.append(
                {
                    "id": msg.id,
                    "sender": sender_name,
                    "sender_id": sender_id,
                    "content": msg.body.content if msg.body else None,
                    "content_type": (
                        str(msg.body.content_type)
                        if msg.body and msg.body.content_type
                        else None
                    ),
                    "created_datetime": (
                        msg.created_date_time.isoformat()
                        if msg.created_date_time
                        else None
                    ),
                },
            )

        return {"success": True, "messages": message_list}

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to get Teams channel messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))
