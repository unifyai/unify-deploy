"""WhatsApp API."""

from communication.whatsapp.views import auth_router, unauth_router

__all__ = ["auth_router", "unauth_router"]
