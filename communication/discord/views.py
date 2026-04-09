"""Discord Comms API endpoints.

All endpoints are admin-authenticated (Bearer token via Orchestra admin key).
Unity calls POST /discord/send for outbound DMs. Orchestra calls
POST /discord/create during assistant contact provisioning.
"""

import json
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request

from communication.discord import bot_manager
from communication.discord.gateway import DISCORD_API_BASE
from common.settings import SETTINGS

logger = logging.getLogger(__name__)

router = APIRouter()


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}


def _bot_headers(bot_token: str) -> dict:
    return {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }


def _load_bot_tokens() -> dict[str, str]:
    """Load bot tokens from the DISCORD_BOT_TOKENS env var.

    Expected format: JSON object mapping bot ID → token, e.g.
    {"123456789": "MTIz...abc", "987654321": "OTg3...xyz"}
    """
    raw = os.environ.get("DISCORD_BOT_TOKENS", "{}")
    return json.loads(raw)


async def _resolve_route(assistant_id: int, contact_discord_id: str) -> dict:
    """Get or create a route for an outbound Discord message.

    Returns the full Orchestra response including ``pool_bot_id``.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SETTINGS.orchestra_url}/admin/discord/route",
            json={
                "assistant_id": assistant_id,
                "contact_number": contact_discord_id,
            },
            headers=_admin_headers(),
            timeout=10.0,
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()


@router.post("/send")
async def send_discord_message(request: Request):
    """Send a DM to a Discord user via the assigned pool bot.

    Body: {
        "to": "<discord_user_id>",
        "body": "<message content>",
        "assistant_id": <int>,
        "media_url": "<optional URL>"
    }
    """
    data = await request.json()
    to = data["to"]
    body = data["body"]
    assistant_id = data["assistant_id"]
    media_url = data.get("media_url")

    route = await _resolve_route(assistant_id, to)
    pool_bot_id = route["pool_bot_id"]

    bot_token = bot_manager.get_bot_token(pool_bot_id)
    if not bot_token:
        raise HTTPException(
            status_code=503,
            detail=f"Bot {pool_bot_id} is not connected",
        )

    headers = _bot_headers(bot_token)

    async with httpx.AsyncClient() as client:
        ch_resp = await client.post(
            f"{DISCORD_API_BASE}/users/@me/channels",
            json={"recipient_id": to},
            headers=headers,
            timeout=10.0,
        )
        if ch_resp.status_code >= 400:
            raise HTTPException(
                status_code=ch_resp.status_code,
                detail=f"Failed to open DM channel: {ch_resp.text}",
            )
        channel_id = ch_resp.json()["id"]

        msg_payload: dict = {"content": body}
        if media_url:
            msg_payload["embeds"] = [{"image": {"url": media_url}}]

        msg_resp = await client.post(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            json=msg_payload,
            headers=headers,
            timeout=10.0,
        )
        if msg_resp.status_code >= 400:
            raise HTTPException(
                status_code=msg_resp.status_code,
                detail=f"Failed to send message: {msg_resp.text}",
            )

    message_id = msg_resp.json()["id"]
    logger.info(f"Sent Discord DM to {to} via bot {pool_bot_id} (msg={message_id})")
    return {"success": True, "message_id": message_id, "channel_id": channel_id}


@router.post("/create")
async def register_bot(request: Request):
    """Register a bot and connect it to the Discord Gateway.

    Called by Orchestra during assistant contact provisioning. Orchestra
    assigns the pool bot via its DAO first, then calls this endpoint to
    ensure the bot has an active Gateway connection.

    Body: {"bot_id": "<discord bot user ID>", "assistant_id": <int>}
    """
    data = await request.json()
    bot_id = data["bot_id"]

    tokens = _load_bot_tokens()
    bot_token = tokens.get(bot_id)
    if not bot_token:
        raise HTTPException(
            status_code=500,
            detail=f"No token configured for bot {bot_id}. "
            "Add it to the DISCORD_BOT_TOKENS env var.",
        )

    await bot_manager.connect_bot(bot_id, bot_token)
    logger.info(f"Bot {bot_id} registered for assistant {data.get('assistant_id')}")
    return {"success": True, "bot_id": bot_id}


@router.delete("/delete")
async def deregister_bot(request: Request):
    """Deregister a bot and disconnect from the Gateway.

    Body: {"bot_id": "<discord bot user ID>"}
    """
    data = await request.json()
    bot_id = data["bot_id"]
    await bot_manager.disconnect_bot(bot_id)
    return {"success": True}


@router.get("/status")
async def bot_status():
    """Health check — return connection status for all pool bots."""
    return bot_manager.get_all_status()
