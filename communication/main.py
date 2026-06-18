"""Comms-app FastAPI entrypoint.

Thin shell that composes communication's private SaaS routers and
startup hooks on top of the droid.gateway aggregator. The 10
external-channel routers (social, phone, gmail, outlook, email,
whatsapp, teams, sharepoint, unillm, discord) are mounted by
``droid.gateway.app.create_app()`` from ``droid.gateway.channels.*``;
this module only adds:

* The /infra/* routers (K8s Job control + SSH tunnel + VM-self
  endpoints) -- communication-private SaaS pieces that don't belong
  in the open-source aggregator.
* Startup hooks that warm up the Kubernetes API client and the
  shared Pub/Sub publisher pool before traffic starts arriving.
* Prometheus metrics instrumentation via the existing
  ``common.metrics.setup_metrics`` (unchanged).

The Discord bot-pool sync + health-check loop is owned by
droid.gateway's built-in lifespan
(``droid.gateway.channels.discord.bot_manager``); no
communication-side bot_manager exists anymore.

Auth wiring:

* /infra/* admin routes: communication's own ``auth_admin_key``
  dependency (str-shaped settings, ``secrets.compare_digest`` vs
  ``SETTINGS.orchestra_admin_key``).
* /infra/* tunnel and vm-self routers: per-route deps (e.g.
  ``authenticate_vm_identity``), declared inside the route handlers
  themselves -- no router-level dep needed.
* All 10 channel routers: droid's ``admin_auth_dependency`` (the
  SecretStr-shaped equivalent reading ``SETTINGS.ORCHESTRA_ADMIN_KEY``
  from the same env var). Both auth functions resolve to the same
  underlying admin key value at runtime.
"""

import json
import logging
from typing import Any

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import Depends

from common.metrics import setup_metrics
from common.settings import SETTINGS
from communication.dependencies import auth_admin_key
from communication.infra.helpers import setup_kubernetes_client
from communication.infra.views import (
    _get_pubsub_clients,
    router as infra_router,
    tunnel_router,
    vm_self_router,
)
from droid.gateway.app import ExtraRouter, create_app
from droid.gateway.context import GatewayContext, default_public_url_provider
from droid.gateway.credentials import EnvCredentialStore
from droid.gateway.envelope_sink import (
    OutboundTransportEnvelopeSink,
    default_topic_suffix,
)
from droid.gateway.outbound_pubsub import PubSubOutboundTransport
from droid.gateway.runtime import RuntimeActivation
from droid.gateway.scheduler import LocalScheduler
from droid.gateway.storage import LocalDiskStorage

load_dotenv(override=True)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)


admin_auth = [Depends(auth_admin_key)]


class CommunicationInfraRuntimeActivator:
    """Activate hosted assistant sessions through Communication infra."""

    def __init__(self, *, base_url: str, admin_key: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._admin_key = admin_key
        self._timeout = timeout

    async def activate(
        self,
        assistant_id: str,
        *,
        reason: str,
        medium: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeActivation:
        assistant = dict((metadata or {}).get("assistant") or {})
        if not assistant:
            return RuntimeActivation(
                activated=False,
                detail="missing-assistant-metadata",
            )

        payload = {
            "api_key": assistant.get("api_key") or SETTINGS.shared_unify_key,
            "medium": medium or reason,
            "assistant_id": assistant_id,
            "user_id": str(assistant.get("user_id") or ""),
            "user_first_name": assistant.get("user_first_name") or "",
            "user_surname": assistant.get("user_last_name") or "",
            "user_email": assistant.get("user_email") or "",
            "assistant_first_name": assistant.get("first_name") or "",
            "assistant_surname": assistant.get("surname") or "",
            "assistant_age": str(assistant.get("age") or ""),
            "assistant_nationality": assistant.get("nationality") or "",
            "assistant_about": assistant.get("about") or "",
            "assistant_job_title": assistant.get("job_title") or "",
            "assistant_timezone": assistant.get("timezone") or "UTC",
            "user_number": assistant.get("user_phone") or "",
            "assistant_number": assistant.get("phone_number") or "",
            "assistant_email": assistant.get("email") or "",
            "assistant_email_provider": assistant.get("email_provider")
            or "google_workspace",
            "user_whatsapp_number": assistant.get("user_whatsapp_number") or "",
            "assistant_whatsapp_number": assistant.get("assistant_whatsapp_number")
            or "",
            "assistant_discord_bot_id": assistant.get("assistant_discord_bot_id") or "",
            "voice_provider": assistant.get("voice_provider") or "",
            "voice_id": assistant.get("voice_id") or "",
            "desktop_mode": assistant.get("desktop_mode") or "none",
            "desktop_url": assistant.get("desktop_url") or "",
            "user_desktops": json.dumps(assistant.get("user_desktops") or []),
            "is_coordinator": str(assistant.get("is_coordinator") or False).lower(),
            "demo_id": assistant.get("demo_id") or "",
            "team_ids": ",".join(str(v) for v in assistant.get("team_ids") or []),
            "team_summaries": json.dumps(assistant.get("team_summaries") or []),
            "self_contact_id": assistant.get("self_contact_id") or 0,
            "boss_contact_id": assistant.get("boss_contact_id") or 0,
            "org_id": assistant.get("organization_id") or "",
            "wake_reasons": json.dumps(
                [{"reason": reason, "medium": medium, "metadata": metadata or {}}],
            ),
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/infra/job/start",
                data=payload,
                headers={"Authorization": f"Bearer {self._admin_key}"},
            )
        response.raise_for_status()
        return RuntimeActivation(activated=True, detail=response.text)


gateway_context = GatewayContext(
    credentials=EnvCredentialStore(),
    storage=LocalDiskStorage(),
    envelope_sink=OutboundTransportEnvelopeSink(
        PubSubOutboundTransport(project_id=SETTINGS.gcp_project_id),
        project_env_suffix=default_topic_suffix(),
    ),
    runtime_activator=CommunicationInfraRuntimeActivator(
        base_url=SETTINGS.comms_url,
        admin_key=SETTINGS.orchestra_admin_key,
    ),
    public_url_provider=default_public_url_provider(),
    scheduler=LocalScheduler(),
)

app = create_app(
    extra_routers=[
        ExtraRouter(infra_router, prefix="/infra", dependencies=admin_auth),
        ExtraRouter(tunnel_router, prefix="/infra"),
        ExtraRouter(vm_self_router, prefix="/infra"),
    ],
    extra_setup_hooks=[setup_kubernetes_client, _get_pubsub_clients],
    gateway_context=gateway_context,
)
setup_metrics(app, service_name="comms")


if __name__ == "__main__":
    uvicorn.run("communication.main:app", host="0.0.0.0", port=8080, reload=True)
