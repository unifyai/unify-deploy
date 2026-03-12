import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from communication.phone.views import (
    auth_router as phone_auth_router,
    unauth_router as phone_unauth_router,
)
from communication.whatsapp.views import router as whatsapp_router
from communication.gmail.views import router as gmail_router
from communication.outlook.views import router as outlook_router
from communication.teams.views import router as teams_router
from communication.infra.views import (
    router as infra_router,
    tunnel_router,
    _get_pubsub_clients,
)
from communication.infra.helpers import setup_kubernetes_client
from communication.social.views import router as social_router
from communication.sharepoint.views import router as sharepoint_router
from communication.unillm import router as unillm_router
from .dependencies import auth_admin_key
from common.metrics import setup_metrics
import uvicorn
from dotenv import load_dotenv

load_dotenv(override=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, setup_kubernetes_client)
    loop.run_in_executor(None, _get_pubsub_clients)
    yield


admin_auth = [Depends(auth_admin_key)]
app = FastAPI(lifespan=lifespan)
setup_metrics(app, service_name="comms")
app.include_router(phone_auth_router, prefix="/phone", dependencies=admin_auth)
app.include_router(phone_unauth_router, prefix="/phone")
app.include_router(whatsapp_router, prefix="/whatsapp", dependencies=admin_auth)
app.include_router(gmail_router, prefix="/gmail", dependencies=admin_auth)
app.include_router(outlook_router, prefix="/outlook", dependencies=admin_auth)
app.include_router(teams_router, prefix="/teams", dependencies=admin_auth)
app.include_router(infra_router, prefix="/infra", dependencies=admin_auth)
app.include_router(tunnel_router, prefix="/infra")
app.include_router(social_router, prefix="/social", dependencies=admin_auth)
app.include_router(sharepoint_router, prefix="/sharepoint", dependencies=admin_auth)
app.include_router(unillm_router, prefix="/unillm")


@app.get("/")
async def read_root():
    return {"message": "success!"}


if __name__ == "__main__":
    uvicorn.run("communication.main:app", host="0.0.0.0", port=8080, reload=True)
