import logging
import os
from datetime import datetime, timezone

import httpx
from azure.core.credentials import AccessToken, TokenCredential
from azure.identity import ClientSecretCredential
from fastapi import HTTPException
from msgraph import GraphServiceClient
from twilio.rest import Client as TwilioClient

from common.settings import SETTINGS

logger = logging.getLogger(__name__)

_GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]


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


def get_admin_graph_client() -> GraphServiceClient:
    """Build a Graph client using tenant-level client credentials.

    Used for provisioning operations (creating/deleting MS365 users,
    assigning licenses) and for mailbox operations on provisioned users
    that don't have per-user OAuth tokens.

    Requires MS365_ADMIN_TENANT_ID, MS365_ADMIN_CLIENT_ID, and
    MS365_ADMIN_CLIENT_SECRET environment variables.
    """
    tenant_id = SETTINGS.ms365_admin_tenant_id
    client_id = SETTINGS.ms365_admin_client_id
    client_secret = os.getenv("MS365_ADMIN_CLIENT_SECRET", "")
    if not all([tenant_id, client_id, client_secret]):
        raise HTTPException(
            status_code=500,
            detail="MS365 admin credentials not configured "
            "(MS365_ADMIN_TENANT_ID, MS365_ADMIN_CLIENT_ID, MS365_ADMIN_CLIENT_SECRET)",
        )
    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    return GraphServiceClient(credentials=credential, scopes=_GRAPH_SCOPES)


async def _lookup_assistant(user_email: str) -> dict:
    """Fetch the first matching assistant record from Orchestra."""
    admin_key = SETTINGS.orchestra_admin_key
    if not admin_key:
        raise HTTPException(
            status_code=500,
            detail="ORCHESTRA_ADMIN_KEY not configured",
        )
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{SETTINGS.orchestra_url}/admin/assistant",
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
    return assistants[0]


def graph_client_from_assistant(assistant: dict, user_email: str) -> GraphServiceClient:
    """Build a Graph client from an already-fetched assistant record.

    Uses per-user OAuth token when available, otherwise falls back to
    tenant-level admin credentials (provisioned accounts).
    """
    access_token = assistant.get("secrets", {}).get("MICROSOFT_ACCESS_TOKEN")
    if access_token:
        return GraphServiceClient(
            credentials=TokenCredentialFromSecret(access_token),
            scopes=_GRAPH_SCOPES,
        )
    logger.info(
        "No per-user OAuth token for %s, using admin credentials",
        user_email,
    )
    return get_admin_graph_client()


async def get_graph_client(user_email: str) -> GraphServiceClient:
    """Get a Graph client for the given assistant mailbox.

    Looks up the assistant from Orchestra to check for per-user OAuth
    tokens (BYOD).  Falls back to tenant-level admin credentials when
    the lookup fails (e.g. during initial provisioning before the
    AssistantContact row exists).
    """
    try:
        assistant = await _lookup_assistant(user_email)
    except Exception:
        logger.warning(
            "Failed to look up assistant for %s, falling back to admin Graph client",
            user_email,
        )
        return get_admin_graph_client()

    return graph_client_from_assistant(assistant, user_email)


async def get_ms_graph_client(
    user_email: str,
) -> tuple[GraphServiceClient, bool]:
    """Get a Graph client along with its credential mode.

    Returns ``(graph, has_user_token)``:
      - ``has_user_token=True``  -> per-user OAuth (BYOD).  ``/me/*`` paths work.
      - ``has_user_token=False`` -> tenant admin credentials (us-provisioned).
        Callers must use ``/users/{email}/*`` paths instead of ``/me/*``.

    This is the Teams/Subscriptions counterpart of
    ``get_outlook_graph_client`` in ``adapters/helpers.py`` — callers that
    build Graph paths need to know which mode they're in.
    """
    try:
        assistant = await _lookup_assistant(user_email)
    except Exception:
        logger.warning(
            "Failed to look up assistant for %s, falling back to admin Graph client",
            user_email,
        )
        return get_admin_graph_client(), False

    access_token = assistant.get("secrets", {}).get("MICROSOFT_ACCESS_TOKEN")
    if access_token:
        return (
            GraphServiceClient(
                credentials=TokenCredentialFromSecret(access_token),
                scopes=_GRAPH_SCOPES,
            ),
            True,
        )
    logger.info(
        "No per-user OAuth token for %s, using admin credentials",
        user_email,
    )
    return get_admin_graph_client(), False


def get_twilio_client():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)


def get_twilio_wa_client():
    account_sid = os.getenv("TWILIO_WA_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_WA_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_WA_ACCOUNT_SID and TWILIO_WA_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)
