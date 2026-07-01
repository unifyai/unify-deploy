"""Microsoft OAuth helpers shared between adapters and comms.

Lives in ``common/`` so neither service has to import the other.  All
functions are thin wrappers over Graph / login.microsoftonline.com plus
Orchestra's secret-storage endpoint — they hold no service-specific
state beyond ``SETTINGS.orchestra_url``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from common.orchestra_secrets import _upsert_assistant_secrets
from common.settings import SETTINGS

logger = logging.getLogger(__name__)


async def exchange_microsoft_code_for_tokens(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    """Exchange an authorization code for tokens."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

        if response.status_code != 200:
            raise Exception(f"Token exchange failed: {response.text}")

        data = response.json()
        data["expires_at"] = (
            datetime.now(tz=timezone.utc)
            + timedelta(seconds=data.get("expires_in", 3600))
        ).isoformat()
        return data


async def get_microsoft_user_info(access_token: str) -> dict:
    """Get user info (email, name, etc.) from an access token."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if response.status_code != 200:
            raise Exception(f"Failed to get user info: {response.text}")

        return response.json()


async def store_microsoft_tokens(
    assistant_id: str,
    new_secrets: dict,
    api_key: str,
    granted_scopes: str = "",
    source: str = "",
    account_email: str = "",
) -> bool:
    """Upsert Microsoft OAuth tokens (and granted scopes) as assistant secrets.

    For each secret, attempt ``PUT /assistant/{id}/secret/{name}`` first
    and fall back to ``POST /assistant/{id}/secret`` on 404.  This keeps
    callers from having to mirror Orchestra's storage state — which the
    backfill flow has no reliable way to do.

    ``source`` records which OAuth flow minted the tokens so the refresh
    scheduler picks the right Azure app registration to redeem the
    refresh token against.  Accepted values:

    - ``"byod"``        — user-consent flow against ``MS365_BYOD_*``
      (multi-tenant Entra ID app; ``tenant_id="common"``).
    - ``"enterprise"``  — authorization-code flow against per-assistant
      ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET``
      secrets.

    The historical ``"unify_ropc"`` source (issued by the now-removed
    ``POST /outlook/create`` and ``POST /outlook/backfill-tokens``
    endpoints) is no longer minted.  Existing rows are still tolerated
    by the refresh scheduler so that any straggler tokens can be cycled
    out, but no new tokens with that source should be written.
    """
    if not SETTINGS.orchestra_url:
        logger.info("SETTINGS.orchestra_url not configured")
        return False
    if not api_key:
        logger.info("api_key not configured")
        return False

    secrets_to_store = {
        "MICROSOFT_ACCESS_TOKEN": new_secrets["access_token"],
        "MICROSOFT_REFRESH_TOKEN": new_secrets.get("refresh_token", ""),
        "MICROSOFT_TOKEN_EXPIRES_AT": new_secrets.get("expires_at", ""),
    }
    if granted_scopes:
        secrets_to_store["MICROSOFT_GRANTED_SCOPES"] = granted_scopes
    if source:
        secrets_to_store["MICROSOFT_TOKEN_SOURCE"] = source
    if account_email:
        secrets_to_store["MICROSOFT_ACCOUNT_EMAIL"] = account_email

    return await _upsert_assistant_secrets(
        assistant_id=assistant_id,
        api_key=api_key,
        secrets=secrets_to_store,
    )
