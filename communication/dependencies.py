import logging
import os

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
    """
    Authenticate an admin key.

    :param request_fastapi: FastAPI request object.
    :param credentials: current authorisation credentials.
    :param db: Database session.
    :raises HTTPException: when admin key is invalid.
    """
    admin_key = credentials.credentials

    # First check if the provided key matches the admin key from environment
    if admin_key == os.environ["ORCHESTRA_ADMIN_KEY"]:
        return

    # If neither condition is met, raise unauthorized exception
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access unauthorized, this incident will be reported.",
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
