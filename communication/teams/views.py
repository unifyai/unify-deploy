import asyncio
import base64
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from msgraph.generated.models.aad_user_conversation_member import (
    AadUserConversationMember,
)
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.channel import Channel
from msgraph.generated.models.channel_membership_type import ChannelMembershipType
from msgraph.generated.models.chat import Chat
from msgraph.generated.models.chat_message import ChatMessage
from msgraph.generated.models.chat_message_attachment import ChatMessageAttachment
from msgraph.generated.models.chat_type import ChatType
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.subscription import Subscription

from communication.helpers import (
    _lookup_assistant,
    get_admin_graph_client,
    get_graph_client,
    graph_client_from_assistant,
)
from common.pubsub import publish_assistant_event
from common.settings import SETTINGS

router = APIRouter()

# Retry once on validation timeouts; Graph occasionally fails to reach
# the webhook on the first attempt while warming up the notification
# pipeline.
MAX_RETRIES = 1

# Cap concurrent subscription POSTs to Graph so we don't stampede a
# user with hundreds of channels.  Graph rate-limits aggressively on
# /subscriptions; 20 in-flight is comfortable and still parallel.
_SUB_CONCURRENCY = 20


def _chats_resource(user_id: str) -> str:
    """Canonical per-user 'all chats' subscription resource.

    Graph documents ``/users/{id}/chats/getAllMessages`` as the form
    that supports delegated permissions; ``/me/...`` also works today
    but subscriptions persist and are refreshed by Graph workers that
    have no ``/me`` request context, so prefer the explicit form.
    """
    return f"/users/{user_id}/chats/getAllMessages"


def _channel_resource(team_id: str, channel_id: str) -> str:
    """Per-channel 'all messages in this channel' subscription resource.

    Delegated + app-only both supported.  No billing ``?model=`` param
    needed.  There is no aggregate per-user channels feed — see
    watch_teams below — so we enumerate joinedTeams → channels and
    create one sub per channel.
    """
    return f"/teams/{team_id}/channels/{channel_id}/messages"


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


def _build_owner_member(upn: str) -> AadUserConversationMember:
    """Build a chat/channel member bound to a user UPN with owner role.

    Graph requires ``user@odata.bind = users/{upn}`` on the member
    payload to attach an existing AAD user; ``roles=["owner"]`` is
    mandatory for both oneOnOne and group chats (and for private /
    shared channel members).
    """
    member = AadUserConversationMember(
        odata_type="#microsoft.graph.aadUserConversationMember",
        roles=["owner"],
    )
    member.additional_data = {
        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{upn}",
    }
    return member


async def _enumerate_user_channels(graph) -> list[tuple[str, str, str]]:
    """Return ``[(team_id, channel_id, display_name), ...]`` for every
    channel the user is a member of, across every joined team.

    Graph has no aggregate "all channels this user is in" resource for
    delegated subscriptions, so we assemble it by walking joinedTeams →
    channels.  Concurrency-bounded to keep latency reasonable on users
    who belong to many teams.
    """
    joined = await graph.me.joined_teams.get()
    teams = list(joined.value or [])

    sem = asyncio.Semaphore(_SUB_CONCURRENCY)

    async def _channels_for(team_id: str) -> list[tuple[str, str, str]]:
        async with sem:
            resp = await graph.teams.by_team_id(team_id).channels.get()
        return [
            (team_id, ch.id, ch.display_name or "")
            for ch in (resp.value or [])
            if ch.id
        ]

    per_team = await asyncio.gather(
        *[_channels_for(t.id) for t in teams if t.id],
        return_exceptions=True,
    )
    out: list[tuple[str, str, str]] = []
    for result in per_team:
        if isinstance(result, Exception):
            logging.warning(f"Failed to list channels for a team: {result}")
            continue
        out.extend(result)
    return out


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


async def _rebuild_teams_watches(
    graph,
    *,
    user_email: str,
    user_id: str,
    webhook_url: str,
) -> dict:
    """Tear down any subs we own for this user and recreate the full set.

    Shared by ``POST /watch`` (full rebuild for a user on demand / on
    renewal) and ``POST /channels`` (so a newly-created channel gets a
    subscription immediately instead of waiting up to 30 min for the
    scheduled renewal).
    """
    webhook_secret = os.getenv("TEAMS_WEBHOOK_SECRET", "unify-teams-webhook")
    client_state = f"{webhook_secret}::{user_email}"

    # Tear down any prior subs we own.  Match by clientState
    # (ownership) + resource shape so we don't disturb subs created
    # by other tenants/apps and so we catch resource path forms
    # Graph normalises after the fact.
    subs = await graph.subscriptions.get()
    for sub in subs.value or []:
        if not _owned_teams_sub(sub, webhook_secret, user_email):
            continue
        try:
            await graph.subscriptions.by_subscription_id(sub.id).delete()
        except Exception:
            pass

    channels = await _enumerate_user_channels(graph)
    channel_resources = [_channel_resource(tid, cid) for tid, cid, _ in channels]

    sem = asyncio.Semaphore(_SUB_CONCURRENCY)

    async def _guarded(resource: str) -> dict:
        async with sem:
            return await _create_one_subscription(
                graph,
                resource=resource,
                webhook_url=webhook_url,
                client_state=client_state,
                user_email=user_email,
            )

    chats_task = asyncio.create_task(_guarded(_chats_resource(user_id)))
    channel_tasks = [asyncio.create_task(_guarded(r)) for r in channel_resources]
    chats_result = await chats_task
    channel_results = await asyncio.gather(*channel_tasks) if channel_tasks else []

    channel_failures = sum(1 for r in channel_results if "error" in r)

    return {
        "success": "subscription_id" in chats_result,
        "chats": chats_result,
        "channels": channel_results,
        "channel_count": len(channel_results),
        "channel_failures": channel_failures,
    }


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


@router.post("/chats")
async def create_teams_chat(request: Request):
    """Create (or return the existing) Teams chat.

    Request body::

        {
          "from": "assistant@yourdomain.com",
          "chat_type": "oneOnOne" | "group",
          "members": ["upn@contoso.com", ...],   # excludes sender
          "topic": "optional (group only)"
        }

    The sender is added implicitly as an owner (Graph requires the
    calling user to be a member).  For ``oneOnOne``, exactly one
    additional member is required; Graph dedupes same-pair calls and
    returns the pre-existing chat_id.  For ``group``, at least two
    additional members are required.

    Returns ``{"success": True, "chat_id": "...", "chat_type": "..."}``.
    """
    data = await request.json()
    sender = data.get("from")
    chat_type_raw = (data.get("chat_type") or "").strip()
    members_in = data.get("members")
    topic = data.get("topic")

    if not sender or not chat_type_raw or not isinstance(members_in, list):
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: from, chat_type, members",
        )
    if chat_type_raw not in ("oneOnOne", "group"):
        raise HTTPException(
            status_code=400,
            detail="chat_type must be 'oneOnOne' or 'group'",
        )
    if chat_type_raw == "oneOnOne" and len(members_in) != 1:
        raise HTTPException(
            status_code=400,
            detail="oneOnOne requires exactly one member (besides sender)",
        )
    if chat_type_raw == "group" and len(members_in) < 2:
        raise HTTPException(
            status_code=400,
            detail="group requires at least two members (besides sender)",
        )
    if chat_type_raw == "oneOnOne" and topic:
        raise HTTPException(
            status_code=400,
            detail="oneOnOne chats cannot have a topic",
        )

    try:
        graph = await get_graph_client(sender)

        me = await graph.me.get()
        sender_upn = me.user_principal_name or sender

        upns: list[str] = []
        seen: set[str] = set()
        for upn in [sender_upn, *members_in]:
            key = (upn or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            upns.append(upn)

        chat = Chat(
            chat_type=(
                ChatType.OneOnOne if chat_type_raw == "oneOnOne" else ChatType.Group
            ),
            members=[_build_owner_member(upn) for upn in upns],
        )
        if chat_type_raw == "group" and topic:
            chat.topic = topic

        result = await graph.chats.post(chat)

        logging.info(
            f"Teams chat created/returned for {sender} "
            f"({chat_type_raw}, {len(upns)} members): {result.id}",
        )
        return {
            "success": True,
            "chat_id": result.id,
            "chat_type": chat_type_raw,
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to create Teams chat: {e}")
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

    Subscriptions are created against the user's delegated token:

    * ``/users/{user-id}/chats/getAllMessages`` — every 1:1 and group
      chat the user participates in, now and in the future.  One
      subscription per user.
    * ``/teams/{team-id}/channels/{channel-id}/messages`` — one
      subscription per channel, for every channel of every team the
      user belongs to.  Graph exposes no aggregate "all channels for
      this user" resource for delegated subs, so we enumerate and
      subscribe per-channel.  New channels are picked up automatically
      on the next 30-min renewal.

    All subscriptions expire in 60 minutes; ``/scheduled/teams-watches``
    re-posts here every 30 min and re-creates them — the re-run
    also re-enumerates channels, so newly joined teams/channels are
    covered without any explicit bookkeeping.

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

        me = await graph.me.get()
        user_id = me.id
        if not user_id:
            raise HTTPException(
                status_code=500,
                detail=f"Graph /me returned no id for {user_email}",
            )

        # ``success`` in the returned dict means the chats sub landed.
        # Per-channel failures don't break delivery of chats and get
        # retried on the next 30-min renewal.  The caller can inspect
        # ``channel_failures`` and the per-channel entries in ``channels``
        # for partial state.
        return await _rebuild_teams_watches(
            graph,
            user_email=user_email,
            user_id=user_id,
            webhook_url=webhook_url,
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to create Teams watch: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _owned_teams_sub(sub, webhook_secret: str, user_email: str) -> bool:
    """Return True if *sub* is one of ours for *user_email*.

    Matches both the chats feed (``/users/{id}/chats/getAllMessages``
    or the older ``/me/chats/getAllMessages``) and per-channel subs
    (``/teams/{id}/channels/{id}/messages``).  ``clientState``
    ownership is always required so we never touch subs owned by
    other tenants/apps.
    """
    if not _sub_owned_by(sub, webhook_secret, user_email):
        return False
    resource = (sub.resource or "").lower()
    if resource.endswith("/chats/getallmessages"):
        return True
    if (
        "/teams/" in resource
        and "/channels/" in resource
        and resource.endswith("/messages")
    ):
        return True
    return False


@router.delete("/watch")
async def delete_teams_watch(request: Request):
    """Delete all unified Teams subscriptions for a user.

    Tears down the per-user chats sub plus every per-channel sub this
    service created on the user's behalf.

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
            if not _owned_teams_sub(sub, webhook_secret, primary_email):
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


_CHANNEL_MEMBERSHIP_TYPES = {
    "standard": ChannelMembershipType.Standard,
    "private": ChannelMembershipType.Private,
    "shared": ChannelMembershipType.Shared,
}


@router.post("/channels")
async def create_teams_channel(request: Request):
    """Create a channel inside an existing team.

    Request body::

        {
          "from": "assistant@yourdomain.com",
          "team_id": "...",
          "display_name": "Launch",
          "description": "optional",
          "membership_type": "standard" | "private" | "shared",  # default "standard"
          "owners": ["upn@..."]   # required iff membership_type != "standard"
        }

    Requires the ``Channel.Create`` delegated scope on the caller; if
    the scope is absent Graph returns a 403 which is surfaced as 500.

    On success, re-posts the full teams-watch set for the sender so the
    new channel gets a subscription immediately instead of waiting up
    to 30 min for the next scheduled renewal.  Rebuild failures are
    logged but do not fail the create.
    """
    data = await request.json()
    sender = data.get("from")
    team_id = data.get("team_id")
    display_name = data.get("display_name")
    description = data.get("description")
    membership_type_raw = (data.get("membership_type") or "standard").strip()
    owners = data.get("owners") or []

    if not sender or not team_id or not display_name:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: from, team_id, display_name",
        )
    if membership_type_raw not in _CHANNEL_MEMBERSHIP_TYPES:
        raise HTTPException(
            status_code=400,
            detail="membership_type must be 'standard', 'private', or 'shared'",
        )
    if not isinstance(owners, list):
        raise HTTPException(
            status_code=400,
            detail="owners must be a list of UPNs when provided",
        )
    if membership_type_raw != "standard" and not owners:
        raise HTTPException(
            status_code=400,
            detail=f"{membership_type_raw} channels require at least one owner",
        )

    try:
        graph = await get_graph_client(sender)

        channel = Channel(
            display_name=display_name,
            description=description,
            membership_type=_CHANNEL_MEMBERSHIP_TYPES[membership_type_raw],
        )
        if membership_type_raw != "standard":
            channel.members = [_build_owner_member(upn) for upn in owners]

        result = await graph.teams.by_team_id(team_id).channels.post(channel)

        logging.info(
            f"Teams channel created by {sender} in team {team_id} "
            f"({membership_type_raw}): {result.id}",
        )

        webhook_url = f"{SETTINGS.adapters_url}/microsoft/router"
        rebuild: dict | None = None
        try:
            me = await graph.me.get()
            if me.id:
                rebuild = await _rebuild_teams_watches(
                    graph,
                    user_email=sender,
                    user_id=me.id,
                    webhook_url=webhook_url,
                )
        except Exception as sub_err:
            # Don't fail the create on a subscription hiccup — the
            # scheduled renewal will pick up the new channel within
            # 30 min, and the caller already has a usable channel_id.
            logging.warning(
                f"Teams watch rebuild after channel create failed: {sub_err}",
            )

        return {
            "success": True,
            "channel_id": result.id,
            "team_id": team_id,
            "membership_type": membership_type_raw,
            "watch_rebuild": rebuild,
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Failed to create Teams channel: {e}")
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


# =============================================================================
# Teams meeting creation
#
# Two endpoints in one: ``POST /teams/create_meeting`` either creates an
# instant Teams meeting (no calendar entry) or schedules a calendar event
# with an attached Teams meeting.  Returns ``join_web_url`` for callers
# (Unity, the browser-automation join flow) to drive into.
# =============================================================================


@router.post("/create_meeting")
async def create_teams_meeting(request: Request):
    """Create a Teams online meeting via Microsoft Graph.

    Two modes selected by ``mode``:

    - ``"instant"`` (default): ``POST /me/onlineMeetings``.  Returns a
      reusable join URL.  ``subject`` / ``start`` / ``end`` are optional.
    - ``"scheduled"``: ``POST /me/events`` with an attached Teams meeting.
      Requires ``subject``, ``start``, ``end``.  Optional ``attendees``
      (list of UPN strings), ``body`` (HTML or plain text rendered as
      HTML), ``location``, ``timezone`` (defaults to ``"UTC"``).

    Request body::

        {
          "assistant_email": "assistant@contoso.com",
          "mode": "instant" | "scheduled",
          "subject": "...",
          "start": "2026-05-01T15:00:00",
          "end": "2026-05-01T16:00:00",
          "timezone": "UTC",
          "attendees": ["alice@example.com", ...],
          "body": "<p>Agenda</p>",
          "location": "Online"
        }

    Returns::

        {
          "success": true,
          "join_web_url": "https://teams.microsoft.com/l/meetup-join/...",
          "meeting_id": "...",        # instant only
          "event_id":   "...",        # scheduled only
          "subject": "...",
          "start": "...",
          "end":   "...",
          "web_link": "https://outlook.office.com/owa/?itemid=..."  # scheduled
        }
    """
    from communication.teams.create_meeting import (
        create_instant_onlinemeeting,
        create_scheduled_meeting_event,
    )

    data = await request.json()
    assistant_email = data.get("assistant_email")
    mode = (data.get("mode") or "instant").strip().lower()

    if not assistant_email:
        raise HTTPException(status_code=400, detail="Missing assistant_email")
    if mode not in ("instant", "scheduled"):
        raise HTTPException(
            status_code=400,
            detail="mode must be 'instant' or 'scheduled'",
        )

    assistant = await _lookup_assistant(assistant_email)
    access_token = (assistant.get("secrets") or {}).get("MICROSOFT_ACCESS_TOKEN") or ""
    if not access_token:
        raise HTTPException(
            status_code=409,
            detail=(
                f"No delegated MICROSOFT_ACCESS_TOKEN for {assistant_email}. "
                "Run the BYOD OAuth flow with OnlineMeetings.ReadWrite "
                "(and Calendars.ReadWrite for scheduled mode)."
            ),
        )

    try:
        if mode == "instant":
            created = await create_instant_onlinemeeting(
                access_token,
                subject=data.get("subject"),
                start_datetime=data.get("start"),
                end_datetime=data.get("end"),
            )
        else:
            subject = data.get("subject")
            start = data.get("start")
            end = data.get("end")
            if not (subject and start and end):
                raise HTTPException(
                    status_code=400,
                    detail="scheduled mode requires subject, start, end",
                )
            created = await create_scheduled_meeting_event(
                access_token,
                subject=subject,
                start_datetime=start,
                end_datetime=end,
                timezone=data.get("timezone") or "UTC",
                attendees=data.get("attendees"),
                body_html=data.get("body"),
                location=data.get("location"),
            )
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logging.error("Graph meeting creation failed: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    assistant_id = str(assistant.get("assistant_id") or "")
    if assistant_id:
        await asyncio.to_thread(
            publish_assistant_event,
            assistant_id=assistant_id,
            thread="teams_meet_created",
            event={
                "assistant_email": assistant_email,
                "join_web_url": created.join_web_url,
                "meeting_id": created.meeting_id,
                "event_id": created.event_id,
                "subject": created.subject,
                "start": created.start_datetime,
                "end": created.end_datetime,
                "mode": mode,
            },
        )

    return {
        "success": True,
        "join_web_url": created.join_web_url,
        "meeting_id": created.meeting_id,
        "event_id": created.event_id,
        "subject": created.subject,
        "start": created.start_datetime,
        "end": created.end_datetime,
        "web_link": created.web_link,
    }
