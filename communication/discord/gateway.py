"""Persistent WebSocket connection to the Discord Gateway API.

Each pool bot maintains one connection. Inbound messages — DMs and guild
channel @mentions — are resolved via Orchestra and published to the
assistant's Pub/Sub topic.
"""

import asyncio
import json
import logging
import platform
import random
import re
import time

import aiohttp
import httpx
from google.cloud import pubsub_v1

from common.settings import SETTINGS

logger = logging.getLogger(__name__)

DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
DISCORD_API_BASE = "https://discord.com/api/v10"

INTENTS_DIRECT_MESSAGES = 1 << 12
INTENTS_GUILD_MESSAGES = 1 << 9
INTENTS_MESSAGE_CONTENT = 1 << 15
BOT_INTENTS = INTENTS_DIRECT_MESSAGES | INTENTS_GUILD_MESSAGES | INTENTS_MESSAGE_CONTENT

# Discord close codes where reconnecting is pointless (config/auth errors).
FATAL_CLOSE_CODES = {4004, 4010, 4011, 4013, 4014}
# Close codes that require a fresh IDENTIFY (session state is invalid).
FRESH_IDENTIFY_CODES = {4003, 4007, 4009}

_pubsub_client: pubsub_v1.PublisherClient | None = None


def _get_pubsub_client() -> pubsub_v1.PublisherClient:
    global _pubsub_client
    if _pubsub_client is None:
        _pubsub_client = pubsub_v1.PublisherClient()
    return _pubsub_client


async def _resolve_discord_route(bot_id: str, sender: str) -> dict | None:
    """Resolve an inbound Discord DM to an assistant via Orchestra.

    Returns one of:
      - {"assistant_id": int, "role": str}   — normal routed message
      - {"action": "auto_reply"}             — decommissioned route
      - {"action": "reject_cold"}            — unknown sender on shared pool
      - None                                 — no route (404)
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SETTINGS.orchestra_url}/admin/discord/resolve",
            params={"bot_id": bot_id, "sender": sender},
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
            timeout=10.0,
        )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.error(f"Discord resolve failed: {resp.status_code} {resp.text}")
        return None
    return resp.json()


async def _fetch_assistant(assistant_id: str) -> dict | None:
    """Fetch assistant data from Orchestra."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SETTINGS.orchestra_url}/admin/assistant",
            params={"agent_id": assistant_id},
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
            timeout=10.0,
        )
    if resp.status_code >= 400:
        logger.error(f"Failed to fetch assistant {assistant_id}: {resp.status_code}")
        return None
    data = resp.json()
    assistants = data.get("info", [])
    if not assistants:
        return None
    return assistants[0]


async def _ensure_job_running(assistant_data: dict, medium: str = "discord") -> None:
    """Fire-and-forget request to start a Unity container for this assistant.

    Calls the comms API's /infra/job/start endpoint, which handles
    deduplication atomically via K8s labels.
    """
    assistant_id = assistant_data.get("assistant_id", "")
    api_key = assistant_data.get("api_key", "")
    if not api_key:
        return

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{SETTINGS.comms_url}/infra/job/start",
                headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
                data={
                    "api_key": api_key,
                    "medium": medium,
                    "assistant_id": assistant_id,
                    "user_id": assistant_data.get("user_id", ""),
                    "user_first_name": assistant_data.get("user_first_name", ""),
                    "user_surname": assistant_data.get("user_surname", ""),
                    "user_email": assistant_data.get("user_email", ""),
                    "assistant_first_name": assistant_data.get(
                        "assistant_first_name",
                        "",
                    ),
                    "assistant_surname": assistant_data.get("assistant_surname", ""),
                    "assistant_age": assistant_data.get("assistant_age", ""),
                    "assistant_nationality": assistant_data.get(
                        "assistant_nationality",
                        "",
                    ),
                    "assistant_about": assistant_data.get("assistant_about", ""),
                    "assistant_timezone": assistant_data.get("assistant_timezone", ""),
                    "user_number": assistant_data.get("user_number", ""),
                    "assistant_number": assistant_data.get("assistant_number", ""),
                    "assistant_email": assistant_data.get("assistant_email", ""),
                    "user_whatsapp_number": assistant_data.get(
                        "user_whatsapp_number",
                        "",
                    ),
                    "assistant_whatsapp_number": assistant_data.get(
                        "assistant_whatsapp_number",
                        "",
                    ),
                    "assistant_discord_bot_id": assistant_data.get(
                        "assistant_discord_bot_id",
                        "",
                    ),
                    "voice_provider": assistant_data.get("voice_provider", ""),
                    "voice_id": assistant_data.get("voice_id", ""),
                    "desktop_mode": assistant_data.get("desktop_mode", "ubuntu"),
                    "user_desktop_mode": assistant_data.get("user_desktop_mode", ""),
                    "user_desktop_filesys_sync": (
                        "true"
                        if assistant_data.get("user_desktop_filesys_sync")
                        else "false"
                    ),
                    "user_desktop_url": assistant_data.get("user_desktop_url", ""),
                    "demo_id": str(assistant_data.get("demo_id", "") or ""),
                    "team_ids": json.dumps(assistant_data.get("team_ids", [])),
                    "org_id": str(assistant_data.get("org_id", "") or ""),
                    "deploy_env": assistant_data.get("deploy_env", ""),
                },
                timeout=5.0,
            )
    except Exception:
        logger.exception(f"Failed to start job for assistant {assistant_id}")


async def _send_dm(bot_token: str, user_id: str, content: str) -> None:
    """Send a DM to a Discord user (for auto-reply / reject messages)."""
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        ch_resp = await client.post(
            f"{DISCORD_API_BASE}/users/@me/channels",
            json={"recipient_id": user_id},
            headers=headers,
            timeout=10.0,
        )
        if ch_resp.status_code >= 400:
            logger.error(f"Failed to open DM channel with {user_id}: {ch_resp.text}")
            return
        channel_id = ch_resp.json()["id"]

        await client.post(
            f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
            json={"content": content},
            headers=headers,
            timeout=10.0,
        )


def _publish_to_pubsub(
    assistant_id: str,
    bot_id: str,
    sender_discord_id: str,
    channel_id: str,
    content: str,
    role: str,
    is_channel: bool = False,
    guild_id: str | None = None,
    attachments: list[dict] | None = None,
) -> None:
    """Publish an inbound Discord message to the assistant's Pub/Sub topic."""
    client = _get_pubsub_client()
    topic_path = client.topic_path(
        SETTINGS.gcp_project_id,
        SETTINGS.assistant_topic(assistant_id),
    )
    payload = {
        "thread": "discord",
        "publish_timestamp": time.time(),
        "event": {
            "bot_id": bot_id,
            "sender_discord_id": sender_discord_id,
            "channel_id": channel_id,
            "body": content,
            "role": role,
            "is_channel": is_channel,
            "guild_id": guild_id,
            "attachments": attachments or [],
        },
    }
    client.publish(
        topic_path,
        json.dumps(payload).encode("utf-8"),
        thread="inbound",
    )
    kind = "channel message" if is_channel else "DM"
    logger.info(
        f"Published Discord {kind} from {sender_discord_id} to assistant {assistant_id}",
    )


class GatewayConnection:
    """Manages a single bot's WebSocket to the Discord Gateway.

    Handles IDENTIFY, heartbeat, resume, reconnect, and dispatches
    inbound DM and guild channel events.
    """

    def __init__(self, bot_id: str, bot_token: str) -> None:
        self.bot_id = bot_id
        self.bot_token = bot_token
        self._session_id: str | None = None
        self._seq: int | None = None
        self._resume_url: str | None = None
        self._heartbeat_interval: float = 41.25
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None
        self._bot_user_id: str | None = None
        self._running = False
        self._heartbeat_acked = True
        self._reconnecting = False
        self._fatal_close_code: int | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed and self._running

    async def start(self) -> None:
        """Connect to the Gateway and begin processing events."""
        self._running = True
        await self._connect(resume=False)

    async def stop(self) -> None:
        """Gracefully disconnect."""
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._receive_task:
            self._receive_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close(code=1000)
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    async def _connect(self, resume: bool = False) -> None:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()

        url = self._resume_url or DISCORD_GATEWAY_URL
        self._ws = await asyncio.wait_for(
            self._http_session.ws_connect(url),
            timeout=30.0,
        )
        self._heartbeat_acked = True

        hello = await asyncio.wait_for(self._ws.receive_json(), timeout=30.0)
        if hello.get("op") != 10:
            logger.error(f"Bot {self.bot_id}: expected HELLO (op 10), got {hello}")
            await self._ws.close(code=1000)
            raise ConnectionError(
                f"Bot {self.bot_id}: did not receive HELLO, got op={hello.get('op')}",
            )
        self._heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0

        if resume and self._session_id:
            await self._send_resume()
        else:
            await self._send_identify()

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _send_identify(self) -> None:
        await self._ws.send_json(
            {
                "op": 2,
                "d": {
                    "token": self.bot_token,
                    "intents": BOT_INTENTS,
                    "properties": {
                        "os": platform.system(),
                        "browser": "unify-comms",
                        "device": "unify-comms",
                    },
                },
            },
        )

    async def _send_resume(self) -> None:
        await self._ws.send_json(
            {
                "op": 6,
                "d": {
                    "token": self.bot_token,
                    "session_id": self._session_id,
                    "seq": self._seq,
                },
            },
        )

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats; reconnect on zombie detection.

        The first heartbeat uses a jittered delay per Discord docs to avoid
        thundering-herd on mass reconnect.
        """
        await asyncio.sleep(self._heartbeat_interval * random.random())
        if not self._running or not self._ws or self._ws.closed:
            return
        await self._ws.send_json({"op": 1, "d": self._seq})

        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            if not self._heartbeat_acked:
                logger.warning(f"Bot {self.bot_id}: heartbeat not ACKed, reconnecting")
                await self._reconnect()
                return
            self._heartbeat_acked = False
            if self._ws and not self._ws.closed:
                await self._ws.send_json({"op": 1, "d": self._seq})

    async def _receive_loop(self) -> None:
        """Read Gateway events and dispatch them."""
        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_event(json.loads(msg.data))
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
                aiohttp.WSMsgType.CLOSING,
            ):
                close_code = self._ws.close_code
                logger.warning(
                    f"Bot {self.bot_id}: WebSocket closed (code={close_code})",
                )
                if close_code in FATAL_CLOSE_CODES:
                    logger.error(
                        f"Bot {self.bot_id}: fatal close code {close_code}, "
                        "not reconnecting",
                    )
                    self._running = False
                    self._fatal_close_code = close_code
                    return
                if not self._running:
                    return
                if close_code in FRESH_IDENTIFY_CODES:
                    self._session_id = None
                    self._seq = None
                    await self._reconnect(resume=False)
                else:
                    await self._reconnect(resume=True)
                return

    async def _handle_event(self, data: dict) -> None:
        op = data.get("op")
        t = data.get("t")
        d = data.get("d")
        s = data.get("s")

        if s is not None:
            self._seq = s

        if op == 11:  # HEARTBEAT_ACK
            self._heartbeat_acked = True
            return

        if op == 1:  # Server requests immediate heartbeat
            if self._ws and not self._ws.closed:
                await self._ws.send_json({"op": 1, "d": self._seq})
            return

        if op == 7:  # RECONNECT
            logger.info(f"Bot {self.bot_id}: received RECONNECT")
            await self._reconnect()
            return

        if op == 9:  # INVALID_SESSION — d indicates if resumable
            resumable = bool(d)
            logger.info(
                f"Bot {self.bot_id}: INVALID_SESSION (resumable={resumable})",
            )
            await asyncio.sleep(2)
            if not resumable:
                self._session_id = None
                self._seq = None
            await self._reconnect(resume=resumable)
            return

        if op == 0:  # DISPATCH
            if t == "READY":
                self._session_id = d["session_id"]
                self._resume_url = d.get("resume_gateway_url")
                self._bot_user_id = d["user"]["id"]
                logger.info(
                    f"Bot {self.bot_id}: READY (session={self._session_id})",
                )
            elif t == "RESUMED":
                logger.info(f"Bot {self.bot_id}: RESUMED")
            elif t == "MESSAGE_CREATE":
                asyncio.create_task(self._handle_message(d))

    async def _handle_message(self, data: dict) -> None:
        """Process an inbound MESSAGE_CREATE (DM or guild channel @mention)."""
        author = data.get("author", {})
        if author.get("bot"):
            return

        guild_id = data.get("guild_id")
        is_channel = guild_id is not None

        if is_channel:
            mentions = data.get("mentions", [])
            if not any(m["id"] == self._bot_user_id for m in mentions):
                return

        sender_id = author["id"]
        content = data.get("content", "")
        channel_id = data["channel_id"]

        if is_channel and self._bot_user_id:
            content = re.sub(
                rf"<@!?{re.escape(self._bot_user_id)}>",
                "",
                content,
            ).strip()

        attachments = [
            {
                "id": a["id"],
                "filename": a["filename"],
                "url": a["url"],
                "content_type": a.get("content_type"),
                "size": a.get("size"),
            }
            for a in data.get("attachments", [])
        ]

        route = await _resolve_discord_route(self.bot_id, sender_id)
        if route is None:
            return

        action = route.get("action")
        if action == "auto_reply":
            if not is_channel:
                await _send_dm(
                    self.bot_token,
                    sender_id,
                    "This bot is no longer active. Please visit console.unify.ai "
                    "to view your assistant details.",
                )
            return
        if action == "reject_cold":
            if not is_channel:
                await _send_dm(
                    self.bot_token,
                    sender_id,
                    "This bot is not accepting new messages.",
                )
            return

        assistant_id = str(route["assistant_id"])
        role = route.get("role", "contact")

        assistant_data = await _fetch_assistant(assistant_id)
        if assistant_data:
            asyncio.create_task(_ensure_job_running(assistant_data))

        _publish_to_pubsub(
            assistant_id=assistant_id,
            bot_id=self.bot_id,
            sender_discord_id=sender_id,
            channel_id=channel_id,
            content=content,
            role=role,
            is_channel=is_channel,
            guild_id=guild_id,
            attachments=attachments,
        )

    async def _reconnect(self, resume: bool = True) -> None:
        """Tear down and re-establish the Gateway connection.

        Uses ``asyncio.current_task()`` to avoid cancelling the calling task
        (e.g. heartbeat loop detecting a zombie cancelling itself before the
        reconnect completes).  A ``_reconnecting`` flag prevents concurrent
        reconnect attempts from racing.
        """
        if self._reconnecting:
            return
        self._reconnecting = True
        try:
            current = asyncio.current_task()
            if self._heartbeat_task and self._heartbeat_task is not current:
                self._heartbeat_task.cancel()
            if self._receive_task and self._receive_task is not current:
                self._receive_task.cancel()
            if self._ws and not self._ws.closed:
                await self._ws.close(code=4000)

            backoff = 1.0
            while self._running:
                try:
                    await self._connect(resume=resume)
                    logger.info(f"Bot {self.bot_id}: reconnected (resume={resume})")
                    return
                except Exception:
                    logger.exception(
                        f"Bot {self.bot_id}: reconnect failed, "
                        f"retrying in {backoff}s",
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
        finally:
            self._reconnecting = False
