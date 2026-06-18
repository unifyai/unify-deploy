import logging
import secrets

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from starlette import status

from common.settings import SETTINGS

logger = logging.getLogger(__name__)

security = HTTPBearer()

VM_IDENTITY_AUDIENCE = "droid-comms-vm"
VM_SA_EMAIL = f"pool-vm-sa@{SETTINGS.vm_project_id}.iam.gserviceaccount.com"


def auth_admin_key(
    request_fastapi: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> None:
    admin_key = credentials.credentials

    expected_key = SETTINGS.orchestra_admin_key
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
            f"{SETTINGS.orchestra_url}/user/basic-info",
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


def authenticate_vm_identity(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """Verify a GCP identity token from a pool VM.

    Returns the verified claims dict containing:
      - email: service account email
      - google.compute_engine.instance_name
      - google.compute_engine.project_id
      - google.compute_engine.zone
    """
    token = credentials.credentials
    try:
        claims = google_id_token.verify_token(
            token,
            google_requests.Request(),
            audience=VM_IDENTITY_AUDIENCE,
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid VM identity token.")

    if claims.get("email") != VM_SA_EMAIL:
        raise HTTPException(
            status_code=403,
            detail="Token is not from an authorized pool VM service account.",
        )

    gce = claims.get("google", {}).get("compute_engine", {})
    if not gce.get("instance_name"):
        raise HTTPException(
            status_code=403,
            detail="Token missing compute engine identity claims.",
        )

    return claims
