from fastapi import Depends, Request, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette import status
import os

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
