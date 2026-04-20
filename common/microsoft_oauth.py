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


async def acquire_microsoft_user_tokens_ropc(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
    scope: str,
) -> dict:
    """Acquire delegated tokens for a service-account mailbox via ROPC.

    Used at provisioning time so unify-managed mailboxes carry the same
    per-user OAuth tokens as BYOD ones — that is the only way to create
    Teams change-notification subscriptions without ``?model=`` (Graph
    requires a billing model on every app-only Teams subscription, but
    rejects the param on delegated subscriptions).

    Requires:
      * the admin app registration to allow public client flows
        (Authentication → Allow public client flows = Yes); ROPC fails
        with ``AADSTS7000218`` otherwise.
      * the target user to be exempt from MFA-requiring Conditional
        Access (the bot mailboxes have no human at the keyboard); fails
        with ``AADSTS50076`` / ``AADSTS53003`` otherwise.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "password",
                "username": username,
                "password": password,
                "scope": scope,
            },
            timeout=30.0,
        )
    if response.status_code != 200:
        raise Exception(f"ROPC token request failed: {response.text}")
    data = response.json()
    data["expires_at"] = (
        datetime.now(tz=timezone.utc) + timedelta(seconds=data.get("expires_in", 3600))
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
    old_secrets: dict,
    new_secrets: dict,
    api_key: str,
    granted_scopes: str = "",
) -> bool:
    """Store Microsoft OAuth tokens (and granted scopes) as assistant secrets."""
    if not SETTINGS.orchestra_url:
        logger.info("SETTINGS.orchestra_url not configured")
        return False

    secrets_to_store = {
        "MICROSOFT_ACCESS_TOKEN": new_secrets["access_token"],
        "MICROSOFT_REFRESH_TOKEN": new_secrets.get("refresh_token", ""),
        "MICROSOFT_TOKEN_EXPIRES_AT": new_secrets.get("expires_at", ""),
    }
    if granted_scopes:
        secrets_to_store["MICROSOFT_GRANTED_SCOPES"] = granted_scopes

    if not api_key:
        logger.info("api_key not configured")
        return False

    success = True
    async with httpx.AsyncClient() as client:
        for secret_name, secret_value in secrets_to_store.items():
            try:
                args = {
                    "url": f"{SETTINGS.orchestra_url}/assistant/{assistant_id}/secret",
                    "json": {"secret_name": secret_name, "secret_value": secret_value},
                    "headers": {"Authorization": f"Bearer {api_key}"},
                    "timeout": 30.0,
                }
                if old_secrets and secret_name in old_secrets:
                    logger.info(
                        f"Updating secret {secret_name} for assistant {assistant_id}",
                    )
                    args["url"] += f"/{secret_name}"
                    args["json"].pop("secret_name")
                    response = await client.put(**args)
                else:
                    logger.info(
                        f"Creating secret {secret_name} for assistant {assistant_id}",
                    )
                    response = await client.post(**args)
                if response.status_code in (200, 201):
                    logger.info(f"Stored {secret_name} for assistant {assistant_id}")
                else:
                    logger.info(
                        f"Failed to store {secret_name}: {response.status_code} - {response.text}",
                    )
                    success = False
            except Exception as e:
                logger.info(f"Error storing {secret_name}: {e}")
                success = False

    return success
