"""Manages the pool of Discord bot Gateway connections.

Bots are registered at runtime via the /discord/create endpoint and
persisted as a local in-memory registry. On service startup, all
previously registered bots are reconnected.
"""

import asyncio
import logging

from communication.discord.gateway import GatewayConnection

logger = logging.getLogger(__name__)

# bot_id → (token, GatewayConnection)
_bots: dict[str, tuple[str, GatewayConnection]] = {}


async def connect_bot(bot_id: str, bot_token: str) -> None:
    """Add a bot to the pool and connect it to the Discord Gateway."""
    if bot_id in _bots:
        existing_conn = _bots[bot_id][1]
        if existing_conn.connected:
            logger.info(f"Bot {bot_id} already connected")
            return
        await existing_conn.stop()

    conn = GatewayConnection(bot_id, bot_token)
    _bots[bot_id] = (bot_token, conn)
    await conn.start()
    logger.info(f"Bot {bot_id} connected to Discord Gateway")


async def disconnect_bot(bot_id: str) -> None:
    """Remove a bot from the pool and close its Gateway connection."""
    entry = _bots.pop(bot_id, None)
    if entry is None:
        return
    _, conn = entry
    await conn.stop()
    logger.info(f"Bot {bot_id} disconnected from Discord Gateway")


def get_bot_token(bot_id: str) -> str | None:
    """Return the token for a connected bot, or None."""
    entry = _bots.get(bot_id)
    return entry[0] if entry else None


def get_all_status() -> dict[str, dict]:
    """Return connection status for every bot in the pool."""
    return {
        bot_id: {
            "connected": conn.connected,
        }
        for bot_id, (_, conn) in _bots.items()
    }


async def health_check() -> None:
    """Reconnect any bots whose Gateway connection has dropped."""
    for bot_id, (token, conn) in list(_bots.items()):
        if not conn.connected:
            logger.warning(f"Bot {bot_id} disconnected, reconnecting")
            new_conn = GatewayConnection(bot_id, token)
            _bots[bot_id] = (token, new_conn)
            await new_conn.start()


async def start_health_check_loop(interval: float = 30.0) -> None:
    """Periodically verify all bots are connected."""
    while True:
        await asyncio.sleep(interval)
        await health_check()
