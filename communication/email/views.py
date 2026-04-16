"""Provider-agnostic email endpoints.

Routes ``/email/send`` and ``/email/attachment`` to the correct
provider-specific handler (Gmail or Outlook) based on the assistant's
stored credentials.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.requests import Request as StarletteRequest

from communication.helpers import _lookup_assistant

router = APIRouter()
logger = logging.getLogger(__name__)


def _is_outlook_assistant(assistant: dict) -> bool:
    """True when the assistant uses Microsoft 365 for email.

    Checks the canonical ``email_provider`` field first, falling back to
    token-sniffing for assistants that predate the field.
    """
    provider = assistant.get("email_provider")
    if provider:
        return provider == "microsoft_365"
    return bool(assistant.get("secrets", {}).get("MICROSOFT_ACCESS_TOKEN"))


async def _clone_request(request: Request, body: bytes) -> StarletteRequest:
    """Build a new ASGI Request with the same headers but a fresh body stream."""
    scope = request.scope.copy()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return StarletteRequest(scope, receive)


@router.post("/send")
async def send_email(request: Request):
    """Send an email, routing to the correct provider.

    Request body must include ``from`` (the assistant's email address).
    The remaining fields (``to``, ``subject``, ``body``, ``cc``, ``bcc``,
    ``in_reply_to``, ``attachment``) are forwarded unchanged to the
    provider-specific handler.
    """
    body_bytes = await request.body()
    import json

    data = json.loads(body_bytes)
    sender = data.get("from")
    if not sender:
        raise HTTPException(status_code=400, detail="Missing 'from' field")

    assistant = await _lookup_assistant(sender)
    forwarded = await _clone_request(request, body_bytes)

    if _is_outlook_assistant(assistant):
        from communication.outlook.views import send_outlook_email

        return await send_outlook_email(forwarded)

    from communication.gmail.views import send_email as gmail_send

    return await gmail_send(forwarded)


@router.get("/attachment")
async def get_attachment(
    receiver_email: str,
    message_id: str,
    attachment_id: str,
    filename: str | None = None,
):
    """Download an attachment, routing to the correct provider.

    Uses ``receiver_email`` to look up the assistant and determine
    which provider-specific attachment endpoint to call.
    """
    assistant = await _lookup_assistant(receiver_email)

    if _is_outlook_assistant(assistant):
        from communication.outlook.views import get_outlook_attachment

        return await get_outlook_attachment(
            user_email=receiver_email,
            message_id=message_id,
            attachment_id=attachment_id,
            filename=filename,
        )

    from communication.gmail.views import get_attachment as gmail_attachment

    return await gmail_attachment(
        receiver_email=receiver_email,
        gmail_message_id=message_id,
        attachment_id=attachment_id,
        filename=filename,
    )
