import asyncio
import logging
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from fastapi import FastAPI, Depends
from communication.phone.views import (
    auth_router as phone_auth_router,
    unauth_router as phone_unauth_router,
)
from communication.whatsapp.views import (
    auth_router as whatsapp_auth_router,
    unauth_router as whatsapp_unauth_router,
)
from communication.gmail.views import router as gmail_router
from communication.outlook.views import router as outlook_router
from communication.teams.views import router as teams_router
from communication.infra.views import (
    router as infra_router,
    tunnel_router,
    vm_self_router,
    _get_pubsub_clients,
)
from communication.infra.helpers import setup_kubernetes_client
from communication.social.views import router as social_router
from common.settings import SETTINGS
from communication.discord.views import router as discord_router
from communication.sharepoint.views import router as sharepoint_router
from communication.unillm import router as unillm_router
from .dependencies import auth_admin_key
from common.metrics import setup_metrics
import logging
import uvicorn
from dotenv import load_dotenv

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)
logger = logging.getLogger(__name__)


async def _connect_discord_pool_bots() -> None:
    """Fetch active Discord pool bots from Orchestra and connect them.

    Called at startup so inbound DMs are handled immediately, even after
    a cold restart of the comms service.
    """
    import httpx
    from communication.discord import bot_manager

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SETTINGS.orchestra_url}/admin/discord/pool",
                params={"include_auth": "true"},
                headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
                timeout=15.0,
            )
        if resp.status_code >= 400:
            logger.warning(f"Failed to fetch Discord pool bots: {resp.status_code}")
            return
        for bot in resp.json():
            if bot.get("auth_token") and bot.get("status") == "active":
                await bot_manager.connect_bot(bot["bot_id"], bot["auth_token"])
    except Exception:
        logger.exception("Error connecting Discord pool bots at startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, setup_kubernetes_client)
    loop.run_in_executor(None, _get_pubsub_clients)

    await _connect_discord_pool_bots()

    from communication.discord.bot_manager import start_health_check_loop

    health_task = asyncio.create_task(start_health_check_loop())
    yield
    health_task.cancel()


admin_auth = [Depends(auth_admin_key)]
app = FastAPI(lifespan=lifespan)
setup_metrics(app, service_name="comms")
app.include_router(phone_auth_router, prefix="/phone", dependencies=admin_auth)
app.include_router(phone_unauth_router, prefix="/phone")
app.include_router(whatsapp_auth_router, prefix="/whatsapp", dependencies=admin_auth)
app.include_router(whatsapp_unauth_router, prefix="/whatsapp")
app.include_router(gmail_router, prefix="/gmail", dependencies=admin_auth)
app.include_router(outlook_router, prefix="/outlook", dependencies=admin_auth)
app.include_router(teams_router, prefix="/teams", dependencies=admin_auth)
app.include_router(infra_router, prefix="/infra", dependencies=admin_auth)
app.include_router(tunnel_router, prefix="/infra")
app.include_router(vm_self_router, prefix="/infra")
app.include_router(social_router, prefix="/social", dependencies=admin_auth)
app.include_router(discord_router, prefix="/discord", dependencies=admin_auth)
app.include_router(sharepoint_router, prefix="/sharepoint", dependencies=admin_auth)
app.include_router(unillm_router, prefix="/unillm")


@app.get("/")
async def read_root():
    return {"message": "success!"}


if __name__ == "__main__":
    uvicorn.run("communication.main:app", host="0.0.0.0", port=8080, reload=True)
