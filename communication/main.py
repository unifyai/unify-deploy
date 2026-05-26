"""Comms-app FastAPI entrypoint.

Thin shell that composes communication's private SaaS routers and
startup hooks on top of the unity.gateway aggregator. The 10
external-channel routers (social, phone, gmail, outlook, email,
whatsapp, teams, sharepoint, unillm, discord) are mounted by
``unity.gateway.app.create_app()`` from ``unity.gateway.channels.*``;
this module only adds:

* The /infra/* routers (K8s Job control + SSH tunnel + VM-self
  endpoints) -- communication-private SaaS pieces that don't belong
  in the open-source aggregator.
* Startup hooks that warm up the Kubernetes API client and the
  shared Pub/Sub publisher pool before traffic starts arriving.
* Prometheus metrics instrumentation via the existing
  ``common.metrics.setup_metrics`` (unchanged).

The Discord bot-pool sync + health-check loop is owned by
unity.gateway's built-in lifespan
(``unity.gateway.channels.discord.bot_manager``); no
communication-side bot_manager exists anymore.

Auth wiring:

* /infra/* admin routes: communication's own ``auth_admin_key``
  dependency (str-shaped settings, ``secrets.compare_digest`` vs
  ``SETTINGS.orchestra_admin_key``).
* /infra/* tunnel and vm-self routers: per-route deps (e.g.
  ``authenticate_vm_identity``), declared inside the route handlers
  themselves -- no router-level dep needed.
* All 10 channel routers: unity's ``admin_auth_dependency`` (the
  SecretStr-shaped equivalent reading ``SETTINGS.ORCHESTRA_ADMIN_KEY``
  from the same env var). Both auth functions resolve to the same
  underlying admin key value at runtime.
"""

import logging

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends

from common.metrics import setup_metrics
from communication.dependencies import auth_admin_key
from communication.infra.helpers import setup_kubernetes_client
from communication.infra.views import (
    _get_pubsub_clients,
    router as infra_router,
    tunnel_router,
    vm_self_router,
)
from unity.gateway.app import ExtraRouter, create_app

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


admin_auth = [Depends(auth_admin_key)]

app = create_app(
    extra_routers=[
        ExtraRouter(infra_router, prefix="/infra", dependencies=admin_auth),
        ExtraRouter(tunnel_router, prefix="/infra"),
        ExtraRouter(vm_self_router, prefix="/infra"),
    ],
    extra_setup_hooks=[setup_kubernetes_client, _get_pubsub_clients],
)
setup_metrics(app, service_name="comms")


if __name__ == "__main__":
    uvicorn.run("communication.main:app", host="0.0.0.0", port=8080, reload=True)
