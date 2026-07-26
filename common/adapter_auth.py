"""Dual auth for hosted adapters endpoints callable by assistant pods.

Mirrors ``unify.gateway.common.auth`` (admin key OR owner-verified user
API key) so pods that present ``UNIFY_KEY`` can hit the same surfaces
Orchestra still calls with ``ORCHESTRA_ADMIN_KEY``. Kept in ``common``
so the adapters Cloud Run image (which does not import ``unify``) can
share the contract with tests.
"""

from __future__ import annotations

import secrets

import httpx
from fastapi import HTTPException, Request

from common.settings import SETTINGS


def extract_bearer_token(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing admin key")
    return auth_header[len("Bearer ") :]


def _is_admin_key(token: str) -> bool:
    expected = SETTINGS.orchestra_admin_key
    return bool(expected) and secrets.compare_digest(token, expected)


async def authenticate_user_api_key(api_key: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{SETTINGS.orchestra_url}/user/basic-info",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid API key.")
    return response.json()


async def require_admin_or_user_key(request: Request) -> None:
    """Accept ``ORCHESTRA_ADMIN_KEY`` or a valid Orchestra user API key.

    Admin callers are marked on ``request.state.adapters_is_admin`` so
    handlers can keep control-plane-only operations locked. User-key
    callers stash the verified key for ownership checks.
    """
    token = extract_bearer_token(request)
    if _is_admin_key(token):
        request.state.adapters_is_admin = True
        return
    info = await authenticate_user_api_key(token)
    request.state.adapters_is_admin = False
    request.state.adapters_api_key = token
    request.state.adapters_user = info


async def require_assistant_ownership(request: Request, assistant_id) -> None:
    """Ensure a user-key caller owns ``assistant_id``; admins bypass."""
    if getattr(request.state, "adapters_is_admin", True):
        return
    denied = HTTPException(
        status_code=403,
        detail="Assistant not owned by caller.",
    )
    api_key = getattr(request.state, "adapters_api_key", "")
    if not api_key or assistant_id in (None, "", "unknown"):
        raise denied
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{SETTINGS.orchestra_url}/assistant",
                params={"agent_id": str(assistant_id)},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10.0,
            )
    except HTTPException:
        raise
    except Exception:
        raise denied
    if response.status_code != 200:
        raise denied
    try:
        assistants = response.json().get("info", [])
    except Exception:
        raise denied
    if not any(str(a.get("agent_id")) == str(assistant_id) for a in assistants):
        raise denied
