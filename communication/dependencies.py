import logging
import os
import secrets

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette import status

from communication.helpers import ORCHESTRA_URL

logger = logging.getLogger(__name__)

security = HTTPBearer()


def auth_admin_key(
    request_fastapi: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
    admin_key = credentials.credentials

    expected_key = os.environ.get("ORCHESTRA_ADMIN_KEY", "")
    if expected_key and secrets.compare_digest(admin_key, expected_key):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access unauthorized.",
    )


async def authenticate_user_api_key(api_key: str) -> dict:
    """
    Validate a user API key against Orchestra's /user/basic-info endpoint.

    Returns the user info dict (contains user_id, email, etc.) on success.
    Raises HTTPException(401) on failure.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{ORCHESTRA_URL}/user/basic-info",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )

    if response.status_code != 200:
        logger.warning(f"API key authentication failed: {response.status_code}")
        raise HTTPException(status_code=401, detail="Invalid API key.")

    return response.json()


def extract_api_key(request: Request) -> str:
    """Extract the Bearer token from the Authorization header."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    raise HTTPException(status_code=401, detail="Missing API key.")
