"""Phone API."""

from communication.phone.views import auth_router, unauth_router

__all__ = ["auth_router", "unauth_router"]
