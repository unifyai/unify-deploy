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

from communication.helpers import (
    _lookup_assistant,
    get_admin_graph_client,
    get_graph_client,
    graph_client_from_assistant,
)
from common.settings import SETTINGS

router = APIRouter()

# Retry once on validation timeouts; Graph occasionally fails to reach
# the webhook on the first attempt while warming up the notification
# pipeline.
MAX_RETRIES = 1

# Resource paths Graph uses for the user-scoped "everything" feeds.  Both
# are delegated-only in our setup — see watch_teams below for why.
_CHATS_RESOURCE = "/me/chats/getAllMessages"
_JOINED_TEAMS_RESOURCE = "/me/joinedTeams/getAllMessages"


def _sub_owned_by(sub, webhook_secret: str, user_email: str) -> bool:
    """Return True if *sub* was created by this service for *user_email*.

    Uses ``clientState`` (``{secret}::{email}``) as the ownership signal
    so we can safely dedupe stale subs without touching subs that belong
    to other apps or users.
    """
    cs = (sub.client_state or "") if hasattr(sub, "client_state") else ""
    if "::" not in cs:
        return False
    prefix, _, email = cs.partition("::")
    return prefix == webhook_secret and email.lower() == user_email.lower()


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


async def _create_one_subscription(
    graph,
    *,
    resource: str,
    webhook_url: str,
    client_state: str,
    user_email: str,
) -> dict:
    """POST a single subscription with our retry behaviour.

    Returns ``{"resource": ..., "subscription_id": ..., "expiration": ...}``
    on success, ``{"resource": ..., "error": ...}`` on a permission
    failure (so the caller can degrade gracefully — e.g. when a BYOD
    tenant hasn't admin-consented ``ChannelMessage.Read.All``, the
    chats sub still succeeds and we continue).
    """
    sub_kwargs = dict(
        change_type="created",
        notification_url=webhook_url,
        resource=resource,
        expiration_date_time=datetime.now(timezone.utc) + timedelta(minutes=60),
        client_state=client_state,
    )
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            result = await graph.subscriptions.post(Subscription(**sub_kwargs))
            logging.info(
                f"Teams watch created on {resource} for {user_email}: {result.id}",
            )
            return {
                "resource": resource,
                "subscription_id": result.id,
                "expiration": result.expiration_date_time.isoformat(),
            }
        except Exception as e:
            last_err = e
            error_str = str(e).lower()
            is_validation_timeout = "validation" in error_str and "timeout" in error_str
            if is_validation_timeout and attempt < MAX_RETRIES:
                logging.warning(
                    f"Teams watch validation timeout on {resource} for "
                    f"{user_email}, retrying...",
                )
                continue
            break
    logging.warning(
        f"Teams watch on {resource} for {user_email} failed: {last_err}",
    )
    return {"resource": resource, "error": str(last_err)}


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
async def watch_teams(request: Request):
    """Create the unified Teams change-notification subscriptions for a user.

    Two subscriptions are created against the user's delegated token:

    * ``/me/chats/getAllMessages`` — every 1:1 and group chat the user
      participates in, now and in the future.
    * ``/me/joinedTeams/getAllMessages`` — every channel message across
      every team the user is a member of, including teams they later
      join.  Requires the tenant admin to have consented
      ``ChannelMessage.Read.All``; if they haven't this sub fails 403
      and we keep going (chats still works).

    Both subscriptions expire in 60 minutes; ``/scheduled/teams-watches``
    re-posts here every 30 min and re-creates them.

    Why delegated-only: app-only Teams subscriptions require a billing
    model declaration (``?model=A`` or ``?model=B``) in the
    notification URL, which Graph charges per notification or per
    licensed user.  Delegated subscriptions inherit the user's existing
    Teams license and need no extra billing setup.  For unify-managed
    mailboxes we obtain delegated tokens via ROPC at provisioning time
    (see ``communication/outlook/views.py#create_outlook_user``).

    Request body::

        { "primary_email": "user@yourdomain.com",
          "webhook_url": "https://..." (optional) }
    """
    data = await request.json()
    user_email = data.get("primary_email")
    webhook_url = data.get("webhook_url") or f"{SETTINGS.adapters_url}/microsoft/router"

    if not user_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    try:
        try:
            assistant = await _lookup_assistant(user_email)
        except HTTPException:
            assistant = None

        has_user_token = bool(
            (assistant or {}).get("secrets", {}).get("MICROSOFT_ACCESS_TOKEN"),
        )
        if not has_user_token:
            # Without a delegated token there is no path forward that
            # avoids ``?model=``.  Surface a clear error rather than
            # silently falling back to app-only — that defeats the whole
            # purpose of the unified watcher.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"No delegated MICROSOFT_ACCESS_TOKEN for {user_email}. "
                    "Run the BYOD OAuth flow or re-provision via "
                    "/outlook/create with assistant_id+api_key so ROPC "
                    "can store user tokens."
                ),
            )

        graph = graph_client_from_assistant(assistant, user_email)

        webhook_secret = os.getenv("TEAMS_WEBHOOK_SECRET", "unify-teams-webhook")
        client_state = f"{webhook_secret}::{user_email}"

        # Tear down any prior subs we own on either of the two unified
        # resources.  We match by clientState (ownership) + suffix so
        # we don't disturb subs created by other tenants/apps and so
        # we catch resource path forms Graph normalises after the fact.
        subs = await graph.subscriptions.get()
        for sub in subs.value or []:
            resource = (sub.resource or "").lower()
            if not (
                resource.endswith("/chats/getallmessages")
                or resource.endswith("/joinedteams/getallmessages")
            ):
                continue
            if not _sub_owned_by(sub, webhook_secret, user_email):
                continue
            try:
                await graph.subscriptions.by_subscription_id(sub.id).delete()
            except Exception:
                pass

        chats_result = await _create_one_subscription(
            graph,
            resource=_CHATS_RESOURCE,
            webhook_url=webhook_url,
            client_state=client_state,
            user_email=user_email,
        )
        channels_result = await _create_one_subscription(
            graph,
            resource=_JOINED_TEAMS_RESOURCE,
            webhook_url=webhook_url,
            client_state=client_state,
            user_email=user_email,
        )

        # Treat the call as successful as long as chats came up — even
        # in well-configured tenants channels can fail because admin
        # consent on ``ChannelMessage.Read.All`` hasn't been granted.
        # The caller can inspect ``channels`` to detect partial failure.
        return {
            "success": "subscription_id" in chats_result,
            "chats": chats_result,
            "channels": channels_result,
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to create Teams watch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/watch")
async def delete_teams_watch(request: Request):
    """Delete both unified Teams subscriptions for a user.

    Request body: { "primary_email": "user@yourdomain.com" }
    """
    data = await request.json()
    primary_email = data.get("primary_email")

    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    try:
        try:
            assistant = await _lookup_assistant(primary_email)
            graph = graph_client_from_assistant(assistant, primary_email)
        except HTTPException:
            graph = get_admin_graph_client()

        webhook_secret = os.getenv("TEAMS_WEBHOOK_SECRET", "unify-teams-webhook")

        subs = await graph.subscriptions.get()
        deleted = 0
        for sub in subs.value or []:
            resource = (sub.resource or "").lower()
            if not (
                resource.endswith("/chats/getallmessages")
                or resource.endswith("/joinedteams/getallmessages")
            ):
                continue
            if not _sub_owned_by(sub, webhook_secret, primary_email):
                continue
            await graph.subscriptions.by_subscription_id(sub.id).delete()
            deleted += 1

        if deleted:
            logging.info(
                f"Teams watch deleted for {primary_email} "
                f"({deleted} subscription(s))",
            )
            return {"success": True, "primary_email": primary_email, "deleted": deleted}

        raise HTTPException(
            status_code=404,
            detail=f"No subscription found for {primary_email}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to delete Teams watch: {e}")
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
