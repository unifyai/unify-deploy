"""Google OAuth helpers shared between adapters and comms.

Lives in ``common/`` so neither service has to import the other.  All
functions are thin wrappers over oauth2.googleapis.com / Google's
userinfo endpoint plus Orchestra's secret-storage endpoint — they hold
no service-specific state beyond ``SETTINGS.orchestra_url``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from common.orchestra_secrets import _upsert_assistant_secrets
from common.settings import SETTINGS

logger = logging.getLogger(__name__)


async def exchange_google_code_for_tokens(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    """Exchange a Google authorization code for access + refresh tokens."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

        if response.status_code != 200:
            raise Exception(f"Google token exchange failed: {response.text}")

        data = response.json()
        data["expires_at"] = (
            datetime.now(tz=timezone.utc)
            + timedelta(seconds=data.get("expires_in", 3600))
        ).isoformat()
        return data


async def refresh_google_tokens(
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> dict:
    """Use a refresh token to obtain a new Google access token."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

        if response.status_code != 200:
            raise Exception(f"Google token refresh failed: {response.text}")

        data = response.json()
        data["expires_at"] = (
            datetime.now(tz=timezone.utc)
            + timedelta(seconds=data.get("expires_in", 3600))
        ).isoformat()
        return data


async def get_google_user_info(access_token: str) -> dict:
    """Fetch the authenticated Google user's profile (email, name, etc.)."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if response.status_code != 200:
            raise Exception(f"Failed to get Google user info: {response.text}")

        return response.json()


async def store_google_tokens(
    assistant_id: str,
    new_secrets: dict,
    api_key: str,
    granted_scopes: str = "",
) -> bool:
    """Upsert Google OAuth tokens (and granted scopes) as assistant secrets.

    See ``common.orchestra_secrets._upsert_assistant_secrets`` for the
    PUT-then-POST upsert semantics.
    """
    if not SETTINGS.orchestra_url:
        logger.info("SETTINGS.orchestra_url not configured")
        return False
    if not api_key:
        logger.info("api_key not configured")
        return False

    secrets_to_store = {
        "GOOGLE_ACCESS_TOKEN": new_secrets["access_token"],
        "GOOGLE_REFRESH_TOKEN": new_secrets.get("refresh_token", ""),
        "GOOGLE_TOKEN_EXPIRES_AT": new_secrets.get("expires_at", ""),
    }
    if granted_scopes:
        secrets_to_store["GOOGLE_GRANTED_SCOPES"] = granted_scopes

    return await _upsert_assistant_secrets(
        assistant_id=assistant_id,
        api_key=api_key,
        secrets=secrets_to_store,
    )
