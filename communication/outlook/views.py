import base64
import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from msgraph.generated.models.assigned_license import AssignedLicense
from msgraph.generated.models.body_type import BodyType
from msgraph.generated.models.email_address import EmailAddress
from msgraph.generated.models.file_attachment import FileAttachment
from msgraph.generated.models.item_body import ItemBody
from msgraph.generated.models.message import Message
from msgraph.generated.models.password_profile import PasswordProfile
from msgraph.generated.models.recipient import Recipient
from msgraph.generated.models.subscription import Subscription
from msgraph.generated.models.user import User
from msgraph.generated.users.item.assign_license.assign_license_post_request_body import (
    AssignLicensePostRequestBody,
)
from msgraph.generated.users.item.messages.item.create_reply.create_reply_post_request_body import (
    CreateReplyPostRequestBody,
)
from msgraph.generated.users.item.messages.item.reply.reply_post_request_body import (
    ReplyPostRequestBody,
)
from msgraph.generated.users.item.send_mail.send_mail_post_request_body import (
    SendMailPostRequestBody,
)

from common.microsoft_oauth import (
    acquire_microsoft_user_tokens_ropc,
    store_microsoft_tokens,
)
from common.scopes import build_scope_string
from communication.helpers import (
    _lookup_assistant,
    get_admin_graph_client,
    get_graph_client,
    graph_client_from_assistant,
)
from common.settings import SETTINGS

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_RETRIES = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_user_node(graph, sender: str, assistant: dict):
    """Return the Graph request builder targeting the correct user.

    Per-user OAuth tokens use ``/me``.  Admin app credentials (provisioned
    accounts) must address ``/users/{email}`` because the token isn't scoped
    to a single user.
    """
    has_user_token = bool(assistant.get("secrets", {}).get("MICROSOFT_ACCESS_TOKEN"))
    if has_user_token:
        return graph.me
    return graph.users.by_user_id(sender)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


@router.post("/create", status_code=201)
async def create_outlook_user(request: Request):
    """Create an MS365 user and assign an Exchange Online license.

    Request body:
    {
        "local": "alice",
        "first_name": "Alice",
        "last_name": "Smith",
        "assistant_id": "..." (optional),
        "api_key": "..."      (optional),
        "features": [...]     (optional, defaults to email + teams)
    }

    The mailbox is provisioned automatically once the license is assigned.
    When ``assistant_id`` and ``api_key`` are provided, an ROPC sign-in
    is performed against the freshly-set password and the resulting
    delegated tokens are stored as assistant secrets.  This lets every
    downstream Graph operation use the per-user OAuth path (``/me/...``)
    rather than the app-only path (``/users/{email}/...``) — which is
    the only way to subscribe to Teams change notifications without the
    ``?model=`` billing param.
    """
    data = await request.json()
    local = data.get("local")
    first_name = data.get("first_name")
    last_name = data.get("last_name")
    assistant_id = data.get("assistant_id")
    api_key = data.get("api_key")
    features = data.get("features") or ["email", "teams"]
    if not local or not first_name or not last_name:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: local, first_name, last_name",
        )

    primary_email = f"{local}@{SETTINGS.ms365_email_domain}"
    password = "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(32)
    )

    graph = get_admin_graph_client()

    # Create the user in Azure AD
    user_body = User(
        account_enabled=True,
        display_name=f"{first_name} {last_name}",
        mail_nickname=local,
        user_principal_name=primary_email,
        password_profile=PasswordProfile(
            force_change_password_next_sign_in=False,
            password=password,
        ),
        given_name=first_name,
        surname=last_name,
        usage_location="US",
    )
    created_user = await graph.users.post(user_body)
    logger.info("Created MS365 user %s (id=%s)", primary_email, created_user.id)

    sku_id = SETTINGS.ms365_license_sku_id
    license_assigned = False
    if sku_id:
        await graph.users.by_user_id(created_user.id).assign_license.post(
            AssignLicensePostRequestBody(
                add_licenses=[AssignedLicense(sku_id=sku_id)],
                remove_licenses=[],
            ),
        )
        logger.info("Assigned license %s to %s", sku_id, primary_email)
        license_assigned = True

    # ROPC: trade the freshly-set password for delegated tokens so the
    # mailbox behaves like a BYOD account from this point on.  Skipped
    # when the caller didn't pass assistant_id+api_key (legacy path:
    # mailbox stays app-only and Teams watch is unreachable — see
    # ``communication/teams/views.py#watch_teams``).
    tokens_stored = False
    if assistant_id and api_key:
        scope = build_scope_string("microsoft", features)
        try:
            tokens = await acquire_microsoft_user_tokens_ropc(
                tenant_id=SETTINGS.ms365_admin_tenant_id,
                client_id=SETTINGS.ms365_admin_client_id,
                client_secret=os.getenv("MS365_ADMIN_CLIENT_SECRET", ""),
                username=primary_email,
                password=password,
                scope=scope,
            )
            tokens_stored = await store_microsoft_tokens(
                assistant_id=assistant_id,
                new_secrets=tokens,
                api_key=api_key,
                granted_scopes=scope,
            )
        except Exception as e:
            logger.error(
                "ROPC token acquisition failed for %s: %s "
                "(check Conditional Access exclusions + 'Allow public "
                "client flows' on the admin app registration)",
                primary_email,
                e,
            )

    # Trigger inbox + Teams watches (best-effort; mailbox may take a few
    # seconds to be fully provisioned by Exchange Online).
    async with httpx.AsyncClient() as http_client:
        await http_client.post(
            f"{SETTINGS.comms_url}/outlook/watch",
            json={"primary_email": primary_email},
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
            timeout=30,
        )
        if tokens_stored:
            await http_client.post(
                f"{SETTINGS.comms_url}/teams/watch",
                json={"primary_email": primary_email},
                headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
                timeout=30,
            )

    return {
        "success": True,
        "user": {"primaryEmail": primary_email},
        "user_id": created_user.id,
        "license_assigned": license_assigned,
        "tokens_stored": tokens_stored,
    }


@router.post("/backfill-tokens")
async def backfill_outlook_tokens(request: Request):
    """Acquire delegated tokens for an already-provisioned bot mailbox.

    Used to retro-fit ROPC onto mailboxes created before
    ``create_outlook_user`` started doing it inline.  Because the
    original generated password was never persisted, the only
    automatable path is: admin-resets the password, then ROPC against
    the new one.

    Request body::

        { "primary_email":  "user@tenant.onmicrosoft.com",
          "assistant_id":   "...",
          "api_key":        "...",
          "features":       [...]   (optional, defaults to email + teams),
          "rotate_password": true   (optional; default true. Set false
                                     only if you've already reset the
                                     password out-of-band and pass it
                                     in via ``password``),
          "password":       "..."   (optional override; required when
                                     rotate_password=false) }

    Side effects:
      * Rotates the mailbox password (Graph ``PATCH /users/{id}``).
        Anyone holding the old one will be locked out.
      * Stores ``MICROSOFT_ACCESS_TOKEN`` / ``MICROSOFT_REFRESH_TOKEN``
        / ``MICROSOFT_TOKEN_EXPIRES_AT`` / ``MICROSOFT_GRANTED_SCOPES``
        on the assistant.
      * Triggers ``/teams/watch`` so the new tokens take effect end-to-end.

    Operational prerequisites are the same as for inline ROPC at
    create-time: the admin app registration must allow public client
    flows, and the mailbox must be exempt from MFA-requiring
    Conditional Access.
    """
    data = await request.json()
    primary_email = data.get("primary_email")
    assistant_id = data.get("assistant_id")
    api_key = data.get("api_key")
    features = data.get("features") or ["email", "teams"]
    rotate_password = data.get("rotate_password", True)
    override_password = data.get("password")

    if not primary_email or not assistant_id or not api_key:
        raise HTTPException(
            status_code=400,
            detail="Missing primary_email / assistant_id / api_key",
        )
    if not rotate_password and not override_password:
        raise HTTPException(
            status_code=400,
            detail="rotate_password=false requires an explicit password",
        )

    graph = get_admin_graph_client()
    user = await graph.users.by_user_id(primary_email).get()
    if not user or not user.id:
        raise HTTPException(
            status_code=404,
            detail=f"No MS365 user for {primary_email}",
        )

    if rotate_password:
        password = "".join(
            secrets.choice(string.ascii_letters + string.digits) for _ in range(32)
        )
        await graph.users.by_user_id(user.id).patch(
            User(
                password_profile=PasswordProfile(
                    force_change_password_next_sign_in=False,
                    password=password,
                ),
            ),
        )
        logger.info("Rotated password for %s in preparation for ROPC", primary_email)
    else:
        password = override_password

    scope = build_scope_string("microsoft", features)
    tokens = await acquire_microsoft_user_tokens_ropc(
        tenant_id=SETTINGS.ms365_admin_tenant_id,
        client_id=SETTINGS.ms365_admin_client_id,
        client_secret=os.getenv("MS365_ADMIN_CLIENT_SECRET", ""),
        username=primary_email,
        password=password,
        scope=scope,
    )
    tokens_stored = await store_microsoft_tokens(
        assistant_id=assistant_id,
        new_secrets=tokens,
        api_key=api_key,
        granted_scopes=scope,
    )

    teams_watch_status: int | None = None
    if tokens_stored:
        async with httpx.AsyncClient() as http_client:
            r = await http_client.post(
                f"{SETTINGS.comms_url}/teams/watch",
                json={"primary_email": primary_email},
                headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
                timeout=30,
            )
            teams_watch_status = r.status_code

    return {
        "success": tokens_stored,
        "primary_email": primary_email,
        "tokens_stored": tokens_stored,
        "password_rotated": rotate_password,
        "teams_watch_status": teams_watch_status,
    }


@router.delete("/delete")
async def delete_outlook_user(request: Request):
    """Delete an MS365 user.  Treats already-absent users as success.

    Request body: { "primary_email": "alice@unify.ai" }
    """
    data = await request.json()
    primary_email = data.get("primary_email")
    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    graph = get_admin_graph_client()
    try:
        await graph.users.by_user_id(primary_email).delete()
        logger.info("Deleted MS365 user %s", primary_email)
        return {
            "success": True,
            "deleted": True,
            "already_absent": False,
            "message": f"User {primary_email} deleted.",
        }
    except Exception as e:
        if "does not exist" in str(e).lower() or "not found" in str(e).lower():
            logger.info("MS365 user %s already absent during delete", primary_email)
            return {
                "success": True,
                "deleted": False,
                "already_absent": True,
                "message": f"User {primary_email} already absent.",
            }
        logger.error("Failed to delete MS365 user %s: %s", primary_email, e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


@router.post("/send")
async def send_outlook_email(request: Request):
    """Send an email via Microsoft Graph API.

    Request body:
    {
        "from": "sender@yourdomain.com",
        "to": "recipient@example.com" or ["r1@example.com", "r2@example.com"],
        "cc": "cc@example.com" (optional),
        "bcc": "bcc@example.com" (optional),
        "subject": "Email subject",
        "body": "Email body content",
        "in_reply_to": "message_id" (optional, for replies),
        "attachment": {"filename": str, "content_base64": str} (optional)
    }
    """
    data = await request.json()
    sender = data.get("from")
    to = data.get("to")
    subject = data.get("subject", "")
    body = data.get("body")
    in_reply_to = data.get("in_reply_to")
    attachment = data.get("attachment")

    if not sender or not to or body is None:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: from, to, body",
        )

    to_list = [to] if isinstance(to, str) else to
    cc = data.get("cc")
    bcc = data.get("bcc")
    cc_list = [cc] if isinstance(cc, str) and cc else (cc or [])
    bcc_list = [bcc] if isinstance(bcc, str) and bcc else (bcc or [])

    try:
        assistant = await _lookup_assistant(sender)
        graph = graph_client_from_assistant(assistant, sender)
        user = await _get_user_node(graph, sender, assistant)

        message = Message(
            subject=subject,
            body=ItemBody(content=body, content_type=BodyType.Text),
            to_recipients=[
                Recipient(email_address=EmailAddress(address=a)) for a in to_list
            ],
        )
        if cc_list:
            message.cc_recipients = [
                Recipient(email_address=EmailAddress(address=a)) for a in cc_list
            ]
        if bcc_list:
            message.bcc_recipients = [
                Recipient(email_address=EmailAddress(address=a)) for a in bcc_list
            ]

        file_attachment = None
        if attachment:
            file_attachment = FileAttachment(
                odata_type="#microsoft.graph.fileAttachment",
                name=attachment.get("filename", "attachment"),
                content_type="application/octet-stream",
                content_bytes=base64.b64decode(
                    attachment.get("content_base64", ""),
                ),
            )

        if in_reply_to:
            if file_attachment:
                draft = await user.messages.by_message_id(
                    in_reply_to,
                ).create_reply.post(
                    CreateReplyPostRequestBody(message=message),
                )
                await user.messages.by_message_id(
                    draft.id,
                ).attachments.post(file_attachment)
                await user.messages.by_message_id(draft.id).send.post()
            else:
                await user.messages.by_message_id(in_reply_to).reply.post(
                    ReplyPostRequestBody(message=message),
                )
        else:
            if file_attachment:
                message.attachments = [file_attachment]
            await user.send_mail.post(
                SendMailPostRequestBody(
                    message=message,
                    save_to_sent_items=True,
                ),
            )

        logger.info("Outlook email sent from %s to %s", sender, to)
        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to send Outlook email: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Watch
# ---------------------------------------------------------------------------


@router.post("/watch")
async def watch_outlook_email(request: Request):
    """Create webhook subscription for new emails in inbox.

    Microsoft Graph subscriptions expire after 3 days max.

    Request body:
    {
        "primary_email": "user@yourdomain.com",
        "webhook_url": "https://..." (optional, defaults to adapters URL)
    }
    """
    data = await request.json()
    user_email = data.get("primary_email")
    webhook_url = data.get("webhook_url") or f"{SETTINGS.adapters_url}/microsoft/router"

    if not user_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    try:
        graph = await get_graph_client(user_email)
        target_resource = f"users/{user_email}/mailFolders/inbox/messages"

        subs = await graph.subscriptions.get()
        for sub in subs.value or []:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                try:
                    await graph.subscriptions.by_subscription_id(sub.id).delete()
                except Exception:
                    pass

        webhook_secret = os.getenv("OUTLOOK_WEBHOOK_SECRET", "unify-outlook-webhook")
        client_state = f"{webhook_secret}::{user_email}"

        for attempt in range(MAX_RETRIES + 1):
            try:
                result = await graph.subscriptions.post(
                    Subscription(
                        change_type="created",
                        notification_url=webhook_url,
                        resource=target_resource,
                        expiration_date_time=datetime.now(timezone.utc)
                        + timedelta(days=3),
                        client_state=client_state,
                    ),
                )
                logger.info("Outlook watch created for %s: %s", user_email, result.id)
                return {
                    "success": True,
                    "subscription_id": result.id,
                    "expiration": result.expiration_date_time.isoformat(),
                }
            except Exception as e:
                error_str = str(e).lower()
                is_validation_timeout = (
                    "validation" in error_str and "timeout" in error_str
                )
                if is_validation_timeout and attempt < MAX_RETRIES:
                    logger.warning(
                        "Outlook watch validation timeout for %s, retrying...",
                        user_email,
                    )
                    continue
                raise

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to create Outlook watch: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/watch")
async def delete_outlook_watch(request: Request):
    """Delete email subscription.

    Request body: { "primary_email": "user@yourdomain.com" }
    """
    data = await request.json()
    primary_email = data.get("primary_email")

    if not primary_email:
        raise HTTPException(status_code=400, detail="Missing primary_email")

    try:
        graph = await get_graph_client(primary_email)
        target_resource = f"users/{primary_email}/mailFolders/inbox/messages"

        subs = await graph.subscriptions.get()
        for sub in subs.value or []:
            if sub.resource and sub.resource.lower() == target_resource.lower():
                await graph.subscriptions.by_subscription_id(sub.id).delete()
                logger.info("Outlook watch deleted for %s", primary_email)
                return {"success": True, "primary_email": primary_email}

        raise HTTPException(
            status_code=404,
            detail=f"No subscription found for {primary_email}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete Outlook watch: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Attachment
# ---------------------------------------------------------------------------


@router.get("/attachment")
async def get_outlook_attachment(
    user_email: str,
    message_id: str,
    attachment_id: str,
    filename: str | None = None,
):
    """Get an attachment from a message."""
    try:
        assistant = await _lookup_assistant(user_email)
        graph = graph_client_from_assistant(assistant, user_email)
        user = await _get_user_node(graph, user_email, assistant)

        attachment = (
            await user.messages.by_message_id(message_id)
            .attachments.by_attachment_id(attachment_id)
            .get()
        )

        if not attachment:
            raise HTTPException(status_code=404, detail="Attachment not found")

        content = getattr(attachment, "content_bytes", None)
        if not content:
            raise HTTPException(status_code=404, detail="Attachment content not found")

        return Response(
            content=content,
            media_type=attachment.content_type or "application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename={filename or attachment.name or 'attachment'}",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get Outlook attachment: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
