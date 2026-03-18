import os
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from twilio.rest import Client as TwilioClient
from azure.core.credentials import AccessToken, TokenCredential
from msgraph import GraphServiceClient


def _get_deploy_env() -> str:
    deploy_env = (os.getenv("DEPLOY_ENV") or "production").strip().lower()
    return deploy_env if deploy_env in {"production", "staging", "preview"} else "production"


DEPLOY_ENV = _get_deploy_env()
ENV_SUFFIX = "" if DEPLOY_ENV == "production" else f"-{DEPLOY_ENV}"


def _cloud_run_url(service_name: str) -> str:
    if service_name.startswith("unity-adapters"):
        return f"https://{service_name}-ky4ja5fxna-uc.a.run.app"
    return f"https://{service_name}-000000000000.us-central1.run.app"


_default_orchestra_url = (
    "https://api.unify.ai/v0"
    if DEPLOY_ENV == "production"
    else "https://internal.example.com/v0"
)
ORCHESTRA_URL = os.getenv("ORCHESTRA_URL", _default_orchestra_url)
ADAPTERS_URL = os.getenv(
    "UNITY_ADAPTERS_URL",
    _cloud_run_url(f"unity-adapters{ENV_SUFFIX}"),
)
COMMS_URL = os.getenv(
    "UNITY_COMMS_URL",
    _cloud_run_url(f"unity-comms-app{ENV_SUFFIX}"),
)


class TokenCredentialFromSecret(TokenCredential):
    """Wraps a stored access token for use with Microsoft Graph SDK."""

    def __init__(self, access_token: str):
        self._token = access_token

    def get_token(self, *scopes, **kwargs) -> AccessToken:
        # Expiry doesn't matter - scheduled job keeps token fresh
        return AccessToken(
            self._token,
            int(datetime.now(tz=timezone.utc).timestamp()) + 3600,
        )


async def get_graph_client(user_email: str) -> GraphServiceClient:
    """Get Graph client using stored access token for the given assistant email."""
    admin_key = os.getenv("ORCHESTRA_ADMIN_KEY")
    if not admin_key:
        raise HTTPException(
            status_code=500,
            detail="ORCHESTRA_ADMIN_KEY not configured",
        )

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{ORCHESTRA_URL}/admin/assistant",
            params={"email": user_email},
            headers={"Authorization": f"Bearer {admin_key}"},
            timeout=30.0,
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=404,
            detail=f"Assistant not found: {user_email}",
        )

    assistants = response.json().get("info", [])
    if not assistants:
        raise HTTPException(
            status_code=404,
            detail=f"Assistant not found: {user_email}",
        )

    secrets = assistants[0].get("secrets", {})
    access_token = secrets.get("MICROSOFT_ACCESS_TOKEN")
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail=f"No Microsoft access token for {user_email}. Complete OAuth first.",
        )

    return GraphServiceClient(
        credentials=TokenCredentialFromSecret(access_token),
        scopes=["https://graph.microsoft.com/.default"],
    )


def get_twilio_client():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)
