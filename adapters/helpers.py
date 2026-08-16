import base64
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from functools import partial
import json
import os
import re
import time
import traceback
import requests
import logging

logger = logging.getLogger(__name__)

NO_DESKTOP_MODE = "none"
# Adapters intentionally cap start-intent waits at the comms edge so webhook
# handlers can return quickly. This is a best-effort handoff, not a durable
# acceptance boundary.
START_INTENT_DISPATCH_TIMEOUT_SECONDS = 0.1


def _runtime_str(value) -> str:
    return "" if value is None else str(value)


from common.metrics import (
    BUILD_WEBHOOK_CONTEXT_DURATION,
    JOB_DEMAND_TOTAL,
    STALE_JOBS_LAST_SWEEP,
    UNITY_JOBS_RUNNING,
    UNITY_JOBS_IDLE,
)
from common.assistant_lookup import get_assistant, managed_desktop_entitled
from common.coordinator_voice import resolve_runtime_voice

from concurrent.futures import ThreadPoolExecutor, as_completed

_WEBHOOK_BG_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook-bg")
from google.cloud import pubsub_v1

from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse

from azure.core.credentials import AccessToken, TokenCredential
from msgraph import GraphServiceClient
from msgraph.generated.users.item.messages.item.message_item_request_builder import (
    MessageItemRequestBuilder,
)

from common.team_summaries_codec import encode_team_summaries_for_form
from common.settings import SETTINGS

LOCAL_ASSISTANT_SELF_CONTACT_ID = 0
LOCAL_ASSISTANT_BOSS_CONTACT_ID = 1

_pubsub_client = None


def _required_contact_id(assistant_data: dict, field_name: str) -> int:
    """Return a resolved contact id required by runtime-facing adapter paths."""
    value = assistant_data.get(field_name)
    if value is None:
        assistant_id = assistant_data.get("assistant_id") or assistant_data.get(
            "agent_id",
        )
        raise ValueError(
            f"Assistant {assistant_id} is missing required {field_name}",
        )
    return int(value)


def _resolve_desktop_mode(assistant_data: dict) -> str:
    """Resolve runtime desktop mode from Orchestra entitlement."""
    if managed_desktop_entitled(assistant_data):
        return str(assistant_data["desktop_mode"])
    return NO_DESKTOP_MODE


def get_pubsub_client():
    global _pubsub_client
    if _pubsub_client is None:
        _pubsub_client = pubsub_v1.PublisherClient()
    return _pubsub_client


def parse_teams_resource_id(resource: str, key: str) -> str | None:
    """
    Extract an ID from a Teams Graph resource path.
    Handles both formats: key('value') and key/value

    Args:
        resource: The Graph API resource path (e.g., "teams('uuid')/channels('19:xxx')")
        key: The key to extract (e.g., "teams", "channels", "chats", "messages")

    Returns:
        The extracted ID or None if not found
    """
    # Format: key('value')
    if f"{key}('" in resource:
        return resource.split(f"{key}('")[1].split("')")[0]
    # Format: /key/value/
    if f"/{key}/" in resource or f"{key}/" in resource:
        parts = resource.split("/")
        try:
            idx = parts.index(key)
            if idx >= 0 and len(parts) > idx + 1:
                return parts[idx + 1]
        except ValueError:
            pass
    return None


def get_contacts(context: str, api_key: str) -> tuple[list[dict[str, str]], int]:
    response = requests.get(
        f"{SETTINGS.orchestra_url}/logs",
        params={"project_name": "Assistants", "context": context},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return response.json(), response.status_code


def get_default_contacts(assistant_data: dict) -> list[dict[str, str]]:
    self_contact_id = _required_contact_id(assistant_data, "self_contact_id")
    boss_contact_id = _required_contact_id(assistant_data, "boss_contact_id")
    return [
        {
            "contact_id": self_contact_id,
            "first_name": assistant_data["assistant_first_name"],
            "surname": assistant_data["assistant_surname"],
            "email_address": assistant_data["assistant_email"],
            "phone_number": assistant_data["assistant_number"],
            "whatsapp_number": assistant_data.get("assistant_whatsapp_number", ""),
            "discord_id": assistant_data.get("assistant_discord_bot_id", ""),
            "slack_user_id": assistant_data.get("assistant_slack_bot_user_id", ""),
            "bio": "",
            "rolling_summary": "",
            "should_respond": False,
            "response_policy": "",
        },
        {
            "contact_id": boss_contact_id,
            "first_name": assistant_data["user_first_name"],
            "surname": assistant_data["user_surname"],
            "email_address": assistant_data["user_email"],
            "phone_number": assistant_data["user_number"],
            "whatsapp_number": assistant_data.get("user_whatsapp_number", ""),
            "discord_id": assistant_data.get("user_discord_id", ""),
            "slack_user_id": assistant_data.get("user_slack_user_id", ""),
            "bio": "",
            "rolling_summary": "",
            "should_respond": True,
            "response_policy": "",
        },
    ]


def _resolve_shared_pool_route(platform: str, pool_id: str, sender: str) -> dict | None:
    """Resolve an inbound message on a shared-pool platform via Orchestra.

    Returns one of:
      - {"assistant_id": int, "role": str} — normal routed message
      - {"action": "auto_reply"}           — decommissioned route
      - {"action": "reject_cold"}          — unknown sender on shared pool
      - None                               — no route at all (404)
    """
    param_name = "bot_id" if platform == "discord" else "pool_number"
    resp = requests.get(
        f"{SETTINGS.orchestra_url}/admin/{platform}/resolve",
        params={param_name: pool_id, "sender": sender},
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.error(
            f"{platform} resolve failed: {resp.status_code} {resp.text}",
        )
        raise RuntimeError(f"{platform} resolve error: {resp.status_code}")
    return resp.json()


def resolve_whatsapp_route(pool_number: str, sender: str) -> dict | None:
    """Resolve an inbound WhatsApp message to an assistant via Orchestra."""
    return _resolve_shared_pool_route("whatsapp", pool_number, sender)


def resolve_phone_route(pool_number: str, sender: str) -> dict | None:
    """Resolve an inbound SMS or PSTN call to an assistant via Orchestra."""
    return _resolve_shared_pool_route("phone", pool_number, sender)


def is_unity_coordinator_email_address(email_address: str | None) -> bool:
    if not email_address:
        return False
    return email_address.strip().lower() == SETTINGS.unity_coordinator_email_address


def is_twin_alias_mailbox(email_address: str | None) -> bool:
    """Whether this is the catch-all mailbox receiving twin alias mail."""
    if not email_address:
        return False
    return email_address.strip().lower() == SETTINGS.unity_twin_alias_mailbox


def is_twin_alias_email_address(email_address: str | None) -> bool:
    """Whether an address lives on the multiplayer twin alias domain."""
    if not email_address:
        return False
    return (
        email_address.strip()
        .lower()
        .endswith(f"@{SETTINGS.unity_twin_alias_email_domain}")
    )


def send_twin_moved_notice(
    gmail_service,
    *,
    mailbox: str,
    to_email: str,
    original_subject: str,
    twin_name: str,
    alias_email: str,
) -> None:
    """Reply from the retired shared address with the twin's new coordinates.

    Sent only to the verified owner during the post-flip grace window (the
    resolver guarantees both), so this cannot loop with strangers or other
    automations: one notice per inbound message, addressed to a human.
    """
    import base64 as _base64
    from email.mime.text import MIMEText as _MIMEText

    name = twin_name or "Your assistant"
    lines = [
        f"{name} has moved to its own email address: {alias_email}",
        "",
        "This shared address no longer reaches it. Please update your",
        f"contacts and resend your message to {alias_email}.",
        "",
        "You can also add a dedicated phone or WhatsApp number for it under",
        "its Contact Details page in the Console.",
    ]
    msg = _MIMEText("\n".join(lines))
    msg["to"] = to_email
    msg["from"] = mailbox
    subject = (original_subject or "").strip()
    msg["subject"] = f"Re: {subject}" if subject else f"{name} has a new address"
    try:
        gmail_service.users().messages().send(
            userId="me",
            body={"raw": _base64.urlsafe_b64encode(msg.as_bytes()).decode()},
        ).execute()

        def _redact(addr: str) -> str:
            local, _, domain = addr.partition("@")
            return f"{local[:2]}***@{domain}" if domain else "***"

        logger.info(
            "Sent twin-moved notice to %s (alias %s)",
            _redact(to_email),
            _redact(alias_email),
        )
    except Exception as exc:
        logger.error("Failed to send twin-moved notice: %s", exc)


def resolve_twin_alias_recipient(last_message: dict) -> str | None:
    """The twin alias address an inbound catch-all delivery was sent to.

    Recipient routing: the alias identifies the twin outright, with no
    sender lookup. X-Gm-Original-To (stamped by the catch-all routing rule)
    is authoritative — it survives BCC deliveries, where To/Cc never carried
    the alias. To/Cc remain as the fallback for messages that predate the
    header option or arrive through paths that strip it.
    """
    original_to = parseaddr(last_message.get("x_gm_original_to") or "")[1]
    original_to = original_to.strip().lower()
    if is_twin_alias_email_address(original_to):
        return original_to
    for field in ("to", "cc"):
        for raw in last_message.get(field) or []:
            addr = parseaddr(raw)[1].strip().lower()
            if is_twin_alias_email_address(addr):
                return addr
    return None


def resolve_email_route(mailbox: str, sender: str) -> dict | None:
    """Resolve an inbound shared-mailbox email to an assistant via Orchestra."""
    resp = requests.get(
        f"{SETTINGS.orchestra_url}/admin/email/resolve",
        params={"mailbox": mailbox, "sender": sender},
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.error(
            f"email resolve failed: {resp.status_code} {resp.text}",
        )
        raise RuntimeError(f"email resolve error: {resp.status_code}")
    return resp.json()


def upsert_whatsapp_call_session(payload: dict) -> dict:
    """Persist the original routing state for a WhatsApp provider call."""
    resp = requests.post(
        f"{SETTINGS.orchestra_url}/admin/whatsapp/call-session",
        json=payload,
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code >= 400:
        logger.error(
            f"WhatsApp call-session upsert failed: {resp.status_code} {resp.text}",
        )
        raise RuntimeError(f"WhatsApp call-session upsert error: {resp.status_code}")
    return resp.json()


def upsert_phone_call_session(payload: dict) -> dict:
    """Persist the original routing state for a PSTN provider call."""
    resp = requests.post(
        f"{SETTINGS.orchestra_url}/admin/phone/call-session",
        json=payload,
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code >= 400:
        logger.error(
            f"Phone call-session upsert failed: {resp.status_code} {resp.text}",
        )
        raise RuntimeError(f"Phone call-session upsert error: {resp.status_code}")
    return resp.json()


def get_phone_call_session(
    provider_call_sid: str,
    provider: str = "twilio",
) -> dict | None:
    """Fetch persisted PSTN call routing state by provider CallSid."""
    resp = requests.get(
        f"{SETTINGS.orchestra_url}/admin/phone/call-session/{provider_call_sid}",
        params={"provider": provider},
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.error(
            f"Phone call-session lookup failed: {resp.status_code} {resp.text}",
        )
        raise RuntimeError(f"Phone call-session lookup error: {resp.status_code}")
    return resp.json()


def update_phone_call_session(payload: dict) -> dict | None:
    """Update status or recording metadata for a PSTN provider call."""
    resp = requests.patch(
        f"{SETTINGS.orchestra_url}/admin/phone/call-session",
        json=payload,
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.error(
            f"Phone call-session update failed: {resp.status_code} {resp.text}",
        )
        raise RuntimeError(f"Phone call-session update error: {resp.status_code}")
    return resp.json()


def assistant_has_active_call(assistant_id: str) -> bool:
    """Report whether an assistant currently has a live or pending PSTN call.

    Best-effort guard used by infra maintenance so a stale-job sweep never
    tears down a runtime that is mid-call. A lookup failure is treated as
    "active" on purpose: it is safer to defer cleanup for one cycle than to
    stop a possibly on-call runtime, and the next sweep re-checks.
    """
    try:
        resp = requests.get(
            f"{SETTINGS.orchestra_url}/admin/phone/active-call",
            params={"assistant_id": assistant_id},
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
            timeout=10,
        )
    except Exception as exc:
        logger.info(
            "[assistant_has_active_call] lookup failed for %s "
            "(treating as active): %s",
            assistant_id,
            exc,
        )
        return True
    if resp.status_code != 200:
        logger.info(
            "[assistant_has_active_call] lookup returned %s for %s "
            "(treating as active)",
            resp.status_code,
            assistant_id,
        )
        return True
    return bool(resp.json().get("active", False))


def get_whatsapp_call_session(
    provider_call_sid: str,
    provider: str = "twilio",
) -> dict | None:
    """Fetch persisted WhatsApp call routing state by provider CallSid."""
    resp = requests.get(
        f"{SETTINGS.orchestra_url}/admin/whatsapp/call-session/{provider_call_sid}",
        params={"provider": provider},
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.error(
            f"WhatsApp call-session lookup failed: {resp.status_code} {resp.text}",
        )
        raise RuntimeError(f"WhatsApp call-session lookup error: {resp.status_code}")
    return resp.json()


def update_whatsapp_call_session(payload: dict) -> dict | None:
    """Update status or recording metadata for a WhatsApp provider call."""
    resp = requests.patch(
        f"{SETTINGS.orchestra_url}/admin/whatsapp/call-session",
        json=payload,
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.error(
            f"WhatsApp call-session update failed: {resp.status_code} {resp.text}",
        )
        raise RuntimeError(f"WhatsApp call-session update error: {resp.status_code}")
    return resp.json()


def resolve_discord_route(bot_id: str, sender: str) -> dict | None:
    """Resolve an inbound Discord DM to an assistant via Orchestra."""
    return _resolve_shared_pool_route("discord", bot_id, sender)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


_SLACK_MAX_SKEW_SECONDS = 60 * 5


def verify_slack_signature(
    *,
    body: bytes,
    timestamp: str,
    signature: str,
) -> bool:
    """Validate a Slack Events API webhook signature.

    Slack signs every webhook with HMAC-SHA256 over
    ``v0:<timestamp>:<raw_body>`` keyed by the app's signing secret.
    Receivers must (a) reject events older than 5 minutes (replay
    protection) and (b) reject mismatched signatures. The signing
    secret is app-level (shared across all workspace installs of the
    Slack app), read from ``SETTINGS.slack_signing_secret``.

    Fails closed on any error: missing headers, stale timestamp,
    missing signing secret, mismatched digest.
    """
    import hashlib as _hashlib
    import hmac as _hmac

    secret = SETTINGS.slack_signing_secret or ""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > _SLACK_MAX_SKEW_SECONDS:
        return False
    basestring = f"v0:{timestamp}:".encode() + body
    expected = (
        "v0="
        + _hmac.new(
            secret.encode(),
            basestring,
            _hashlib.sha256,
        ).hexdigest()
    )
    return _hmac.compare_digest(expected, signature)


_SLACK_SEEN: dict[str, float] = {}
_SLACK_SEEN_TTL = 300.0


def slack_message_already_seen(message_key: str) -> bool:
    """Best-effort in-process dedup for inbound Slack messages.

    A single channel mention is delivered twice by Slack -- once as
    ``app_mention`` and once as ``message`` -- with *distinct* ``event_id``s
    but the *same* ``client_msg_id``. Keying on that stable id stops us from
    double-dispatching to Orchestra and double-publishing to Pub/Sub.

    Best-effort only: Cloud Run runs multiple instances and the pair can land
    on different ones, so the authoritative dedup is Unity-side (a single
    subscription consumer per assistant). This just trims the common case.
    """
    if not message_key:
        return False
    now = time.time()
    cutoff = now - _SLACK_SEEN_TTL
    for k in [k for k, t in _SLACK_SEEN.items() if t < cutoff]:
        del _SLACK_SEEN[k]
    if message_key in _SLACK_SEEN:
        return True
    _SLACK_SEEN[message_key] = now
    return False


SLACK_API_BASE = "https://slack.com/api"


def _post_slack_dispatch(dispatch_body: dict) -> dict | None:
    """POST to Orchestra's Slack dispatch; ``None`` on 404 / transport error."""
    resp = requests.post(
        f"{SETTINGS.orchestra_url}/admin/slack/dispatch",
        json=dispatch_body,
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.error(
            f"slack dispatch failed: {resp.status_code} {resp.text}",
        )
        return None
    return resp.json()


def _resolve_slack_bot_token(team_id: str) -> str | None:
    """Fetch the workspace bot token from Orchestra (admin auth)."""
    resp = requests.get(
        f"{SETTINGS.orchestra_url}/admin/slack/install",
        params={"slack_team_id": team_id, "include_token": True},
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code >= 400:
        return None
    return resp.json().get("bot_access_token") or None


def fetch_slack_user_profile(team_id: str, slack_user_id: str) -> dict:
    """Resolve a Slack sender's profile via ``users.info`` (best-effort).

    Returns ``{email, real_name, display_name}`` with ``None`` values when
    the lookup fails or the bot lacks the ``users:read[.email]`` scope. The
    caller re-dispatches regardless, so a missing profile simply degrades
    to the provisional org-Coordinator route.
    """
    empty = {"email": None, "real_name": None, "display_name": None}
    bot_token = _resolve_slack_bot_token(team_id)
    if not bot_token:
        return empty
    resp = requests.get(
        f"{SLACK_API_BASE}/users.info",
        params={"user": slack_user_id},
        headers={"Authorization": f"Bearer {bot_token}"},
        timeout=10,
    )
    payload = resp.json()
    if not payload.get("ok"):
        logger.warning(f"slack users.info failed: {payload.get('error')}")
        return empty
    user = payload.get("user") or {}
    profile = user.get("profile") or {}
    return {
        "email": profile.get("email") or None,
        "real_name": user.get("real_name") or profile.get("real_name") or None,
        "display_name": profile.get("display_name") or None,
    }


def resolve_slack_inbound(payload: dict) -> dict | None:
    """Route a Slack Events API ``event_callback`` via Orchestra.

    Slack routing is structurally richer than the ``(pool_id, sender)``
    shape of Discord/WhatsApp pools: Orchestra needs the full Events
    API envelope (``team_id``, ``event.channel``, ``event.thread_ts``,
    ``event.text``, ``event.user``) to consult per-workspace installs,
    per-channel bindings, persistent thread/DM routes, and the
    ``<@app> <token>`` addressing convention.

    Coordinator routing in an org workspace is *personal to the sender*:
    each member owns their own workspace Coordinator. When the first pass
    returns ``needs_sender_identity``, we resolve the sender's profile via
    ``users.info`` and re-dispatch so the message pins to the sender's own
    Coordinator. The first-pass route is already valid, so any failure
    resolving identity degrades to that provisional route.

    Returns one of:

    * ``{"assistant_id": int, "is_channel": bool, "bot_user_id": str,
       "routing_metadata": dict}`` -- normal route.
    * ``{"drop": True}`` -- bot echo, retry, or unsupported event type.
    * ``None`` -- 404 or transport failure.
    """
    # Flatten the Slack envelope into Orchestra's ``DispatchRequest``
    # shape. ``channel_type`` is present on ``message`` events
    # ('im'/'channel'/'group'/'mpim') but absent on ``app_mention``
    # (always in a channel); fall back to the channel-id prefix
    # ('D' = direct message) so both event types map correctly.
    event = payload.get("event") or {}
    channel_id = event.get("channel", "") or ""
    channel_type = event.get("channel_type") or (
        "im" if channel_id.startswith("D") else "channel"
    )
    team_id = payload.get("team_id", "") or event.get("team", "")
    sender_slack_user_id = event.get("user", "") or ""
    dispatch_body = {
        "slack_team_id": team_id,
        "channel_id": channel_id,
        "channel_type": channel_type,
        "sender_slack_user_id": sender_slack_user_id,
        "text": event.get("text", "") or "",
        "event_ts": event.get("event_ts", "") or event.get("ts", ""),
        "thread_ts": event.get("thread_ts"),
    }
    data = _post_slack_dispatch(dispatch_body)
    if data is None:
        return None

    if data.get("needs_sender_identity") and team_id and sender_slack_user_id:
        profile = fetch_slack_user_profile(team_id, sender_slack_user_id)
        second = _post_slack_dispatch(
            {
                **dispatch_body,
                "sender_email": profile.get("email"),
                "sender_real_name": profile.get("real_name"),
                "sender_display_name": profile.get("display_name"),
                "sender_identity_provided": True,
            },
        )
        if second is not None:
            data = second

    # Translate Orchestra's ``DispatchResponse`` into the adapter's
    # routing dict. ``handled=False`` (no install / bot echo / unbound
    # channel) becomes a drop. ``is_channel`` isn't carried by Orchestra,
    # so derive it from the channel type we sent.
    if not data.get("handled"):
        return {"drop": True}
    return {
        "assistant_id": data.get("assistant_id"),
        "is_channel": channel_type != "im",
        "bot_user_id": data.get("bot_user_id", "") or "",
        "routing_metadata": data.get("routing_metadata") or {},
    }


# =============================================================================
# Microsoft Teams (Bot Framework) inbound
# =============================================================================
#
# Additive to the existing delegated-Graph Teams integration: the Graph
# change-notification path handles a user's own 1:1 chats via their OAuth
# tokens, while this Bot-Framework path handles bot-addressed channel /
# group-chat activity and installs into the org's own Teams app.
#
# Inbound trust is a hand-rolled Bot Framework JWT check (PyJWT + the Bot
# Connector JWKS), the same "verify-the-provider-signature-ourselves" shape
# as the Slack HMAC verifier above -- we deliberately avoid the heavy Bot
# Framework SDK.

_MS_TEAMS_BOT_OPENID_CONFIG_URL = (
    "https://login.botframework.com/v1/.well-known/openidconfiguration"
)
_MS_TEAMS_BOT_ISSUER = "https://api.botframework.com"
_MS_TEAMS_BOT_JWK_CLIENT_TTL_SECONDS = 24 * 3600

_ms_teams_bot_jwk_client_cache = None
_ms_teams_bot_jwk_client_built_at = 0.0


class MsTeamsBotAuthError(Exception):
    """Raised when an inbound Bot Framework activity JWT fails verification."""


def _ms_teams_bot_jwks_uri() -> str:
    resp = requests.get(_MS_TEAMS_BOT_OPENID_CONFIG_URL, timeout=10)
    resp.raise_for_status()
    jwks_uri = resp.json().get("jwks_uri")
    if not jwks_uri:
        raise MsTeamsBotAuthError("Bot Connector OpenID config has no jwks_uri.")
    return jwks_uri


def _ms_teams_bot_jwk_client(force_refresh: bool = False):
    global _ms_teams_bot_jwk_client_cache, _ms_teams_bot_jwk_client_built_at
    from jwt import PyJWKClient

    now = time.time()
    stale = (
        now - _ms_teams_bot_jwk_client_built_at > _MS_TEAMS_BOT_JWK_CLIENT_TTL_SECONDS
    )
    if _ms_teams_bot_jwk_client_cache is None or stale or force_refresh:
        _ms_teams_bot_jwk_client_cache = PyJWKClient(_ms_teams_bot_jwks_uri())
        _ms_teams_bot_jwk_client_built_at = now
    return _ms_teams_bot_jwk_client_cache


def verify_ms_teams_bot_token(token: str) -> dict:
    """Verify an inbound Bot Framework JWT and return its claims.

    Validates RS256 signature (against the Bot Connector JWKS), ``iss``
    (Bot Connector emitter), ``aud`` (our bot app id from
    ``SETTINGS.ms_teams_bot_app_id``), and ``exp``. Raises
    :class:`MsTeamsBotAuthError` on any failure. Signing keys rotate, so a
    missing ``kid`` triggers one JWKS refresh + retry.
    """
    import jwt as _jwt

    app_id = SETTINGS.ms_teams_bot_app_id or ""
    if not token:
        raise MsTeamsBotAuthError("Missing bearer token.")
    if not app_id:
        raise MsTeamsBotAuthError("MS_TEAMS_BOT_APP_ID is not configured.")

    def _decode(client) -> dict:
        signing_key = client.get_signing_key_from_jwt(token)
        return _jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=app_id,
            issuer=_MS_TEAMS_BOT_ISSUER,
            options={"require": ["exp", "iss", "aud"]},
        )

    try:
        return _decode(_ms_teams_bot_jwk_client())
    except _jwt.PyJWKClientError:
        try:
            return _decode(_ms_teams_bot_jwk_client(force_refresh=True))
        except Exception as exc:  # noqa: BLE001 - normalize to our error type
            raise MsTeamsBotAuthError(str(exc)) from exc
    except _jwt.InvalidTokenError as exc:
        raise MsTeamsBotAuthError(str(exc)) from exc


def _strip_ms_teams_bot_mention(
    text: str,
    entities: list[dict],
    recipient_id: str,
) -> tuple[bool, str]:
    """Return ``(bot_mentioned, text_without_bot_mention)``.

    Teams marks @mentions with ``mention`` entities carrying the exact
    ``<at>Name</at>`` substring; the bot's own mention is the one whose
    ``mentioned.id`` equals the activity ``recipient.id``.
    """
    bot_mentioned = False
    cleaned = text or ""
    for entity in entities or []:
        if entity.get("type") != "mention":
            continue
        if (entity.get("mentioned") or {}).get("id") == recipient_id:
            bot_mentioned = True
            mention_text = entity.get("text") or ""
            if mention_text:
                cleaned = cleaned.replace(mention_text, "")
    return bot_mentioned, cleaned.strip()


def _post_ms_teams_bot(path: str, body: dict) -> dict | None:
    """POST to an Orchestra admin endpoint; ``None`` on 404 / transport error."""
    resp = requests.post(
        f"{SETTINGS.orchestra_url}{path}",
        json=body,
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        timeout=10,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.error(
            f"ms_teams_bot POST {path} failed: {resp.status_code} {resp.text}",
        )
        return None
    return resp.json()


def ensure_ms_teams_bot_pending_install(activity: dict) -> dict | None:
    """Record a pending (unbound) install when the bot is added to a tenant.

    Captures ``serviceUrl`` (required for every future outbound call) so the
    tenant-to-org bind handshake can complete later without another inbound
    round-trip. Returns Orchestra's install response (carrying ``bind_nonce``
    and a one-click ``connect_url``) so the caller can DM the installer the
    connect link; ``None`` on a missing tenant or transport failure.
    """
    channel_data = activity.get("channelData") or {}
    tenant_id = (channel_data.get("tenant") or {}).get("id") or ""
    if not tenant_id:
        return None
    installer = activity.get("from") or {}
    return _post_ms_teams_bot(
        "/admin/ms-teams-bot/pending-install",
        {
            "tenant_id": tenant_id,
            "bot_app_id": SETTINGS.ms_teams_bot_app_id,
            "service_url": activity.get("serviceUrl") or "",
            "installer_aad_object_id": installer.get("aadObjectId") or "",
        },
    )


def claim_ms_teams_bot_welcome(install_id: int, conversation_id: str) -> bool:
    """Claim the one-shot install welcome for a conversation via Orchestra.

    The bot-add ``conversationUpdate`` is redelivered by Teams / the Bot
    Connector, so gating the greeting on a server-side claim keyed by
    ``(install_id, conversation_id)`` is what stops the welcome from repeating.
    The id is normalized first: Teams appends ``;messageid=…`` to a channel
    conversation id on some deliveries of the same bot-add, and the claim matches
    on the exact string, so a raw id lets one install win two claims and greet
    twice. Returns ``True`` only when this call won the claim and should send the
    welcome; ``False`` on an already-welcomed conversation or any transport
    failure — failing closed keeps a flaky call from re-spamming the greeting.
    """
    resp = _post_ms_teams_bot(
        "/admin/ms-teams-bot/welcome-claim",
        {
            "install_id": install_id,
            "conversation_id": (conversation_id or "").split(";")[0],
        },
    )
    if not resp:
        return False
    return bool(resp.get("claimed"))


# The Azure bot is a *single-tenant* registration in Unify's home tenant
# (``MS365_ADMIN_TENANT_ID``), so Connector tokens are minted from that
# tenant's authority — not the shared ``botframework.com`` authority. One
# minted token (valid ~24h) authenticates outbound into every customer tenant
# the bot is installed in; a tiny process-local cache avoids a mint per send.
_MS_TEAMS_BOT_CONNECTOR_SCOPE = "https://api.botframework.com/.default"
_ms_teams_bot_connector_token_cache: dict[str, tuple[str, float]] = {}
_MS_TEAMS_BOT_TOKEN_SKEW_SECONDS = 300


def _mint_ms_teams_bot_connector_token() -> str | None:
    """Mint (or reuse) a Bot Connector token via client credentials.

    Returns ``None`` (best-effort) when the bot credentials or home tenant are
    unconfigured, or the mint fails — the welcome DM is a nicety, never a hard
    dependency of recording the install.
    """
    app_id = SETTINGS.ms_teams_bot_app_id or ""
    app_secret = SETTINGS.ms_teams_bot_app_secret or ""
    tenant_id = SETTINGS.ms365_admin_tenant_id or ""
    if not app_id or not app_secret or not tenant_id:
        logger.warning(
            "ms_teams_bot: connector token mint skipped — missing app "
            "credentials or MS365_ADMIN_TENANT_ID",
        )
        return None

    cached = _ms_teams_bot_connector_token_cache.get(app_id)
    now = time.time()
    if cached is not None and cached[1] - _MS_TEAMS_BOT_TOKEN_SKEW_SECONDS > now:
        return cached[0]

    try:
        resp = requests.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_secret,
                "scope": _MS_TEAMS_BOT_CONNECTOR_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
    except Exception:
        logger.exception("ms_teams_bot: connector token mint transport error")
        return None
    if resp.status_code >= 400:
        logger.error(
            f"ms_teams_bot: connector token mint failed: "
            f"{resp.status_code} {resp.text}",
        )
        return None
    payload = resp.json() or {}
    token = payload.get("access_token") or ""
    if not token:
        return None
    expires_in = int(payload.get("expires_in") or 3600)
    _ms_teams_bot_connector_token_cache[app_id] = (token, now + expires_in)
    return token


# Canonical Teams app name. Must match the app title/short name in the Teams
# manifest and Partner Center listing, and the Bot Framework registration, so
# the bot identifies itself consistently in every response (Teams Store
# certification requires the welcome/bot copy to name the app exactly as the
# manifest does). This is the *app* name; "Unify" on its own still refers to the
# Unify account/platform the workspace connects to, which is a distinct noun.
_MS_TEAMS_BOT_APP_NAME = "Unify T-W1N"

# Onboarding pathways surfaced in the welcome (Teams Store certification wants a
# new user to have a clear route to sign up, get help, and reach support without
# leaving the greeting). These are the canonical public entry points used across
# the product; keep them in sync with the store long description.
_MS_TEAMS_BOT_SIGNUP_URL = "https://console.unify.ai"
_MS_TEAMS_BOT_HELP_URL = "https://docs.unify.ai"
_MS_TEAMS_BOT_SUPPORT_URL = "https://unify.ai/contact"

# AI-transparency disclosure. Certification requires content generated by AI to
# be clearly indicated. It belongs on the onboarding surfaces that introduce the
# assistant, and at most **once** per activity: live assistant replies are
# labelled natively instead — ``unify.gateway.channels.ms_teams_bot`` attaches
# the schema.org ``AIGeneratedContent`` entity on every send, which Teams renders
# as its own "AI generated" caption. Repeating the sentence within a message (or
# on every message) reads as duplicated copy to a reviewer.
_MS_TEAMS_BOT_AI_DISCLOSURE = (
    "Responses are generated by AI and may be inaccurate — please review before "
    "relying on them."
)

# Single statement of what the app is for, reused across the welcome and help
# surfaces so the value proposition never drifts between them.
_MS_TEAMS_BOT_VALUE_PROP = (
    f"I'm {_MS_TEAMS_BOT_APP_NAME}, your AI teammate inside Microsoft Teams. "
    "Ask me questions, hand off work, and get updates without leaving Teams."
)

# One-line nudge for a workspace that has not been bound to a Unify owner yet.
# Short by design: a message answering "hi" must not restate the whole welcome.
_MS_TEAMS_BOT_CONNECT_NUDGE = (
    "This workspace isn't connected to a Unify account yet, so I can only help "
    "you finish setup — tap **Connect to Unify** below."
)

# The bot's deterministic command vocabulary. Certification requires the generic
# commands to be answered *distinctly* — from each other and from unrecognised
# input — and requires every reply to offer a way forward, so classification
# drives which card gets sent rather than one canned response for all input.
_MS_TEAMS_BOT_COMMAND_GREETING = "greeting"
_MS_TEAMS_BOT_COMMAND_HELP = "help"
_MS_TEAMS_BOT_COMMAND_OTHER = "other"

_MS_TEAMS_BOT_GREETING_PHRASES = frozenset(
    {
        "hi",
        "hii",
        "hiya",
        "hey",
        "heya",
        "hello",
        "hi there",
        "hey there",
        "hello there",
        "good morning",
        "good afternoon",
        "good evening",
    },
)

_MS_TEAMS_BOT_HELP_PHRASES = frozenset(
    {
        "help",
        "commands",
        "command",
        "what can you do",
        "what can you do for me",
    },
)


def classify_ms_teams_bot_command(text: str) -> str:
    """Classify mention-stripped message text into the command vocabulary.

    Exact-phrase matching only: ``help`` is the help command, while "help me
    draft this email" is work for the assistant. That precision is what lets the
    help command be answered deterministically even on a connected workspace
    without stealing real requests from the assistant.
    """
    normalized = " ".join((text or "").split()).lower()
    normalized = normalized.lstrip("/").strip(" !?.,;:'\"")
    if normalized in _MS_TEAMS_BOT_GREETING_PHRASES:
        return _MS_TEAMS_BOT_COMMAND_GREETING
    if normalized in _MS_TEAMS_BOT_HELP_PHRASES:
        return _MS_TEAMS_BOT_COMMAND_HELP
    return _MS_TEAMS_BOT_COMMAND_OTHER


def _ms_teams_bot_action_buttons(connect_url: str) -> list[dict]:
    """Onboarding action set for the bot cards.

    ``Connect to Unify`` (the nonce-carrying bind link) is only shown while the
    install is still pending — an already-bound tenant has no ``connect_url`` and
    must not be told to reconnect. The sign-up / help / support buttons are
    always present so a new user has a route forward regardless of bind state
    (Teams Store certification: clear onboarding pathways in the welcome).
    """
    actions: list[dict] = []
    if connect_url:
        actions.append(
            {
                "type": "Action.OpenUrl",
                "title": "Connect to Unify",
                "url": connect_url,
            },
        )
    actions.extend(
        [
            {
                "type": "Action.OpenUrl",
                "title": "Sign up",
                "url": _MS_TEAMS_BOT_SIGNUP_URL,
            },
            {
                "type": "Action.OpenUrl",
                "title": "Help & docs",
                "url": _MS_TEAMS_BOT_HELP_URL,
            },
            {
                "type": "Action.OpenUrl",
                "title": "Contact support",
                "url": _MS_TEAMS_BOT_SUPPORT_URL,
            },
        ],
    )
    return actions


def _ms_teams_bot_card(
    title: str,
    paragraphs: list[str],
    *,
    notes: tuple[str, ...] = (),
    connect_url: str = "",
    ai_disclosure: bool = False,
) -> dict:
    """Compose the bot's Adaptive Card attachment.

    Every bot-authored message is card-*only*. Teams renders an activity's
    ``text`` and its card attachment as two stacked blocks, so carrying the same
    copy in both made a single message read as two — the Store "combine them into
    a single welcome message" failure. ``fallbackText`` covers renderers that
    cannot draw a card without adding a second visible block in Teams.

    ``notes`` render subtle (limitations / account dependency) and
    ``ai_disclosure`` appends the transparency line at most once.
    """
    body: list[dict] = [
        {
            "type": "TextBlock",
            "size": "Medium",
            "weight": "Bolder",
            "wrap": True,
            "text": title,
        },
    ]
    body.extend(
        {"type": "TextBlock", "wrap": True, "text": paragraph}
        for paragraph in paragraphs
    )
    body.extend(
        {"type": "TextBlock", "wrap": True, "isSubtle": True, "text": note}
        for note in notes
    )
    if ai_disclosure:
        body.append(
            {
                "type": "TextBlock",
                "wrap": True,
                "isSubtle": True,
                "spacing": "Small",
                "text": _MS_TEAMS_BOT_AI_DISCLOSURE,
            },
        )
    card = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "fallbackText": " ".join([title, *paragraphs]),
        "body": body,
        "actions": _ms_teams_bot_action_buttons(connect_url),
    }
    return {
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": card,
    }


def _ms_teams_bot_welcome_card(connect_url: str) -> dict:
    """The install greeting — the only message that welcomes.

    Carries what Teams Store certification expects in a bot welcome: the value
    proposition, the one-time setup step, the account dependency, the AI
    disclosure, and onboarding buttons. Command replies deliberately use
    different titles and copy so a reviewer never sees the welcome twice.

    The setup wording follows the bind state: an already-bound tenant has no
    ``connect_url`` and must not be told to connect.
    """
    paragraphs = [_MS_TEAMS_BOT_VALUE_PROP]
    if connect_url:
        paragraphs.append(
            "To finish setup, tap **Connect to Unify** and sign in — you only do "
            "this once.",
        )
    paragraphs.append("Type **Help** anytime to see everything I can do.")
    notes = (
        (
            (
                "Requires an active Unify account or organization. Until this "
                "workspace is connected, I can only help you finish setup."
            )
            if connect_url
            else "Requires an active Unify account or organization."
        ),
    )
    return _ms_teams_bot_card(
        f"Welcome to {_MS_TEAMS_BOT_APP_NAME}",
        paragraphs,
        notes=notes,
        connect_url=connect_url,
        ai_disclosure=True,
    )


def _ms_teams_bot_greeting_card(connect_url: str) -> dict:
    """Reply to "hi" / "hello" — a short hello, not a second welcome."""
    paragraphs = [_MS_TEAMS_BOT_VALUE_PROP]
    if connect_url:
        paragraphs.append(_MS_TEAMS_BOT_CONNECT_NUDGE)
    paragraphs.append("Type **Help** to see everything I can do.")
    return _ms_teams_bot_card(
        "Hi there",
        paragraphs,
        connect_url=connect_url,
    )


def _ms_teams_bot_help_card(connect_url: str) -> dict:
    """Reply to "help" — the value proposition plus every supported command.

    Certification requires help to be self-explanatory about what the app is for
    and to list the commands the bot answers, with no dead ends.
    """
    paragraphs = [_MS_TEAMS_BOT_VALUE_PROP]
    if connect_url:
        paragraphs.append(_MS_TEAMS_BOT_CONNECT_NUDGE)
    paragraphs.extend(
        [
            "Here's what I answer to:",
            "**Help** — show this message.",
            "**Hi** or **Hello** — a quick hello and where to start.",
            (
                "**Anything else** — just describe what you need in your own "
                "words and I'll take it from there."
            ),
        ],
    )
    return _ms_teams_bot_card(
        f"What {_MS_TEAMS_BOT_APP_NAME} can do",
        paragraphs,
        notes=("In a channel or group chat, @mention me so I see the message.",),
        connect_url=connect_url,
        ai_disclosure=True,
    )


def _ms_teams_bot_unknown_command_card(connect_url: str) -> dict:
    """Reply to input the bot does not recognise, always with a way forward.

    The apology is the title only — restating it in the body is the kind of
    duplicated sentence the Store review flagged.
    """
    paragraphs = []
    if connect_url:
        paragraphs.append(_MS_TEAMS_BOT_CONNECT_NUDGE)
    paragraphs.append("Type **Help** to see all the commands I can work with.")
    return _ms_teams_bot_card(
        "Sorry, I didn't understand that",
        paragraphs,
        connect_url=connect_url,
    )


def _send_ms_teams_bot_message(
    activity: dict,
    install: dict | None,
    message: dict,
) -> None:
    """POST a proactive activity into the inbound conversation.

    Best-effort: any missing piece (``service_url``, ``conversation_id``, no
    connector token, or a send failure) is logged and swallowed so it never
    breaks the webhook. ``service_url`` falls back to the stored install so a
    reply still lands when the triggering activity omits it.
    """
    conversation = activity.get("conversation") or {}
    conversation_id = conversation.get("id") or ""
    service_url = (
        activity.get("serviceUrl") or (install or {}).get("service_url") or ""
    ).rstrip("/")
    if not conversation_id or not service_url:
        logger.warning(
            "ms_teams_bot: proactive send skipped — missing conversation_id or "
            "service_url",
        )
        return
    token = _mint_ms_teams_bot_connector_token()
    if not token:
        return
    try:
        resp = requests.post(
            f"{service_url}/v3/conversations/{conversation_id}/activities",
            json=message,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
    except Exception:
        logger.exception("ms_teams_bot: proactive send transport error")
        return
    if resp.status_code >= 400:
        logger.error(
            f"ms_teams_bot: proactive send failed: {resp.status_code} {resp.text}",
        )


def send_ms_teams_bot_install_welcome(
    activity: dict,
    install: dict | None,
) -> None:
    """Proactively DM the installer a welcome + one-click connect link.

    The caller is responsible for firing this once per conversation (a Teams add
    can emit repeated events). Card-only: an activity carrying both ``text`` and
    a card renders as two stacked blocks, which reads as two welcome messages.
    The ``Connect to Unify`` button appears only while the install is pending
    (Orchestra returns a ``connect_url``); the link itself comes from Orchestra,
    the single source of the Console URL + nonce.
    """
    if not install:
        return
    connect_url = install.get("connect_url") or ""
    message: dict = {
        "type": "message",
        "attachments": [_ms_teams_bot_welcome_card(connect_url)],
    }
    _send_ms_teams_bot_message(activity, install, message)


def send_ms_teams_bot_command_reply(
    activity: dict,
    command: str,
    connect_url: str | None,
) -> None:
    """Answer a generic bot command with the card specific to that command.

    Keeps the bot responsive without repeating the install welcome: a greeting, a
    help request, and unrecognised input each get their own title and copy
    (certification requires valid and invalid commands to be answered
    differently). ``connect_url`` is set only for a still-unbound workspace, and
    is what adds the setup nudge and the ``Connect to Unify`` button.
    """
    connect_url = connect_url or ""
    if command == _MS_TEAMS_BOT_COMMAND_HELP:
        card = _ms_teams_bot_help_card(connect_url)
    elif command == _MS_TEAMS_BOT_COMMAND_GREETING:
        card = _ms_teams_bot_greeting_card(connect_url)
    else:
        card = _ms_teams_bot_unknown_command_card(connect_url)
    _send_ms_teams_bot_message(
        activity,
        None,
        {"type": "message", "attachments": [card]},
    )


def revoke_ms_teams_bot_install(activity: dict) -> None:
    """Soft-revoke the install when the bot is removed from a tenant.

    Teams sends a ``conversationUpdate`` carrying the bot itself in
    ``membersRemoved`` when the app is uninstalled from the tenant. We hold
    no per-tenant token to revoke at Microsoft, so mirroring that removal
    into Orchestra (marking the install revoked, dropping routes) is the only
    teardown we can perform — it stops inbound routing to a bot that is no
    longer installed.
    """
    channel_data = activity.get("channelData") or {}
    tenant_id = (channel_data.get("tenant") or {}).get("id") or ""
    if not tenant_id:
        return
    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}
    try:
        resp = requests.get(
            f"{SETTINGS.orchestra_url}/admin/ms-teams-bot/install",
            params={"tenant_id": tenant_id},
            headers=headers,
            timeout=10,
        )
    except Exception:
        logger.exception(
            "ms_teams_bot: failed to resolve install for tenant %s",
            tenant_id,
        )
        return
    if resp.status_code == 404:
        return
    if resp.status_code >= 400:
        logger.error(
            f"ms_teams_bot GET install failed: {resp.status_code} {resp.text}",
        )
        return
    install_id = (resp.json() or {}).get("id")
    if not install_id:
        return
    try:
        del_resp = requests.delete(
            f"{SETTINGS.orchestra_url}/admin/ms-teams-bot/install/{install_id}",
            headers=headers,
            timeout=10,
        )
    except Exception:
        logger.exception("ms_teams_bot: failed to revoke install %s", install_id)
        return
    if del_resp.status_code >= 400:
        logger.error(
            f"ms_teams_bot DELETE install {install_id} failed: "
            f"{del_resp.status_code} {del_resp.text}",
        )


def resolve_ms_teams_bot_inbound(activity: dict) -> dict | None:
    """Route a Teams ``message`` activity via Orchestra.

    The sender's display name is present inline on the activity, so we
    resolve identity in a single dispatch pass (``sender_identity_provided``)
    -- no ``users.info``-style second pass is needed for name matching
    (unlike Slack). Email-based matching via the Teams roster is deferred.

    Returns Orchestra's ``DispatchResponse`` dict (``handled``,
    ``assistant_id``, ``routing_metadata``, ...), or ``None`` on 404 /
    transport failure.
    """
    channel_data = activity.get("channelData") or {}
    tenant_id = (channel_data.get("tenant") or {}).get("id") or ""
    conversation = activity.get("conversation") or {}
    sender = activity.get("from") or {}
    recipient = activity.get("recipient") or {}
    bot_mentioned, addressed_text = _strip_ms_teams_bot_mention(
        activity.get("text", "") or "",
        activity.get("entities") or [],
        recipient.get("id") or "",
    )
    conversation_reference = {
        "bot": recipient,
        "user": sender,
        "conversation": conversation,
        "channelId": activity.get("channelId") or "msteams",
        "serviceUrl": activity.get("serviceUrl") or "",
        "tenantId": tenant_id,
    }
    return _post_ms_teams_bot(
        "/admin/ms-teams-bot/dispatch",
        {
            "tenant_id": tenant_id,
            "conversation_id": conversation.get("id") or "",
            "conversation_type": conversation.get("conversationType") or "personal",
            "channel_id": (channel_data.get("channel") or {}).get("id"),
            "sender_aad_object_id": sender.get("aadObjectId") or "",
            "sender_display_name": sender.get("name") or "",
            "bot_mentioned": bot_mentioned,
            "addressed_text": addressed_text,
            "conversation_reference": json.dumps(conversation_reference),
            "sender_identity_provided": True,
        },
    )


def _normalize_display_name(name: str) -> str:
    """Lower-case + strip punctuation/diacritics so two display strings
    that "look the same" compare equal.

    Conservative: only does what's safe for an automated match.  We do
    NOT collapse cultural ordering differences (``Surname, First`` vs
    ``First Surname``) — callers handle that by trying both join orders.
    """
    if not name:
        return ""
    import unicodedata as _ud

    decomposed = _ud.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not _ud.combining(c))
    cleaned = re.sub(r"[^\w\s]", " ", stripped).lower()
    return re.sub(r"\s+", " ", cleaned).strip()


def _match_contact_by_name(
    sender_name: str,
    contacts: list[dict],
) -> dict | None:
    """Return the unique contact whose ``first_name + surname`` matches
    ``sender_name``, or ``None`` when there is zero or >1 match.

    Two candidates per contact: ``"First Surname"`` and ``"Surname First"``
    — Teams display names sometimes follow the locale convention of the
    sender's tenant.  Ambiguity (multiple matches) is treated as a
    *failed* match: we'd rather drop the message than route it to the
    wrong contact.
    """
    target = _normalize_display_name(sender_name)
    if not target:
        return None
    matches: list[dict] = []
    for contact in contacts:
        first = contact.get("first_name", "") or ""
        surname = contact.get("surname", "") or ""
        candidates = (
            _normalize_display_name(f"{first} {surname}"),
            _normalize_display_name(f"{surname} {first}"),
        )
        if target in candidates:
            matches.append(contact)
    if len(matches) != 1:
        if len(matches) > 1:
            logger.info(
                f"ambiguous_name_match: {sender_name!r} matched "
                f"{len(matches)} contacts; refusing to route",
            )
        return None
    return matches[0]


def check_contact_details(
    email_address: str = None,
    phone_number: str = None,
    medium: str = None,
    user_number: str = None,
    user_whatsapp_number: str = None,
    user_email: str = None,
) -> bool:
    """
    Check if the contact details are valid.

    WhatsApp is handled separately via Orchestra's resolve endpoint and
    never reaches this function (the adapter passes validate_contact=False
    for WhatsApp).

    Args:
        email_address: The email address of the contact.
        phone_number: The phone number of the contact.
        medium: The medium of the contact.
        user_number: The phone number of the user.
        user_whatsapp_number: The whatsapp number of the user.
        user_email: The email of the user.
    """
    logger.info(
        f"Checking contact details: {email_address}, {phone_number}, {medium}, "
        f"{user_number}, {user_whatsapp_number}, {user_email}",
    )
    if medium in ("email", "teams") and user_email == email_address:
        return True
    if medium in ("msg", "phone") and user_number == phone_number:
        return True
    return False


def check_valid_contact(
    email_address: str = None,
    phone_number: str = None,
    medium: str = None,
    assistant_context: str = None,
    api_key: str = None,
    user_number: str = None,
    user_whatsapp_number: str = None,
    user_email: str = None,
    assistant_data: dict = None,
    sender_name: str = None,
) -> tuple[list[dict], bool, dict | None]:
    """
    Check if the contact is valid.

    Returns:
        ``(contacts, is_valid, matched_contact)`` where ``matched_contact``
        is the specific contact row the caller resolved to (or ``None``
        when no single contact was pinned — e.g. boss / default path,
        ambiguous name fallback, or invalid).

    Args:
        email_address: The email address of the contact.
        phone_number: The phone number of the contact.
        medium: The medium of the contact.
        assistant_context: The context of the assistant.
        api_key: The API key of the assistant.
        user_number: The phone number of the user.
        user_whatsapp_number: The whatsapp number of the user.
        user_email: The email of the user.
        assistant_data: The data of the assistant.
        sender_name: Display name from the inbound provider (Teams/etc.).
            Used as a fallback when we have no resolvable email — e.g.
            federated, anonymous-guest, or consumer-MSA Teams users
            whose ``from.user.email`` is missing.  Match is best-effort
            and only succeeds when exactly one contact's name lines up.
    """
    logger.info(
        f"Checking valid contact: {email_address}, {phone_number}, {medium}, "
        f"{user_number}, {user_whatsapp_number}, {user_email}, {assistant_context}",
    )

    # check for contact in assistant contacts
    context = f"{assistant_context}/Contacts"
    response_json, status_code = get_contacts(context, api_key)
    default_contacts = get_default_contacts(assistant_data)
    resp_contacts = response_json["logs"] if status_code == 200 else []
    if len(resp_contacts) < 2:
        # if the context or project isn't created yet (first time user)
        # len(resp_contacts) < 2 is to deal with race conditions right on
        # hiring a new assistant, whenever the wakeup message is sent, the contact
        # manager gets initialized in unity so there's a stage where the context is
        # created but the contacts haven't been added yet
        if (
            status_code != 200
            and response_json.get("detail")
            in [
                "Project Assistants not found.",
                f"Context '{context}' not found",
            ]
        ) or len(resp_contacts) < 2:
            # check for boss user
            if check_contact_details(
                email_address=email_address,
                phone_number=phone_number,
                medium=medium,
                user_number=user_number,
                user_whatsapp_number=user_whatsapp_number,
                user_email=user_email,
            ):
                logger.info(
                    f"Boss user found: {email_address}, {phone_number}, {medium}, "
                    f"{user_number}, {user_whatsapp_number}, {user_email}",
                )
                return default_contacts, True, None

        # otherwise
        logger.info(f"Failed to get contacts for assistant {assistant_context}")
        logger.info(response_json)
        return default_contacts, False, None
    contacts = [c["entries"] for c in resp_contacts]
    logger.info(f"Contacts: {contacts}")
    if len(contacts) == 0:
        return default_contacts, False, None

    # check for boss user
    boss_contact_id = _required_contact_id(assistant_data, "boss_contact_id")
    boss_contact = [
        contact for contact in contacts if contact["contact_id"] == boss_contact_id
    ]
    logger.info(f"Boss contact: {boss_contact}")
    if len(boss_contact) > 0:
        boss_contact = boss_contact[0]
        boss_user_number = boss_contact.get("phone_number", "")
        boss_user_email = boss_contact.get("email_address", "")
        if check_contact_details(
            email_address=email_address,
            phone_number=phone_number,
            medium=medium,
            user_number=boss_user_number,
            user_whatsapp_number=user_whatsapp_number,
            user_email=boss_user_email,
        ):
            logger.info(
                f"Boss user found: {email_address}, {phone_number}, {medium}, "
                f"{boss_user_number}, {user_whatsapp_number}, {boss_user_email}",
            )
            return contacts, True, boss_contact
    else:
        logger.info("No boss user found")
        return default_contacts, False, None

    # check all contacts
    for contact in contacts:
        if check_contact_details(
            email_address=email_address,
            phone_number=phone_number,
            medium=medium,
            user_number=contact.get("phone_number", ""),
            user_whatsapp_number=assistant_data["user_whatsapp_number"],
            user_email=contact.get("email_address", ""),
        ):
            logger.info(f"Contact found: {contact}")
            return contacts, True, contact

    # Teams-specific fallback: federated / consumer / anonymous-guest
    # senders often arrive with no resolvable email (or our synthesised
    # ``{id}@teams`` placeholder).  In that case the only signal Graph
    # gives us is ``from.user.displayName``.  Match it against the
    # contacts' first/surname; refuse to route on ambiguity.
    if (
        medium == "teams"
        and sender_name
        and (not email_address or email_address.endswith("@teams"))
    ):
        match = _match_contact_by_name(sender_name, contacts)
        if match is not None:
            logger.info(f"Contact matched by name: {sender_name!r} -> {match}")
            return contacts, True, match

    return default_contacts, False, None


def _sanitize_assistant_label(value: str) -> str:
    """Normalize an assistant id to the form used on the K8s ``assistant-id`` label."""
    return value.strip().lower().replace("_", "-")


def classify_stale_running(
    stale_running: list[dict],
    session_states: dict[str, dict],
) -> tuple[dict[str, str], list[str], list[str]]:
    """Decide the fate of each stale *running* job. Pure: no IO.

    For every stale running job, use the pre-read session state to choose one of
    three outcomes:

    - **stop** the owning session — the job is still the session's current
      binding, the session is non-terminal, is not already stopping, and the
      assistant has no live/pending call. Returned in ``assistants_to_stop``
      (``{assistant_id: job_name}``).
    - **safe-delete** the job directly — no session owns it anymore (missing,
      terminal, or bound to a different job), or the job carries no usable
      assistant id. Returned in ``safe_delete_names``.
    - **defer** — leave it for a later sweep: inspection failed, the session is
      already stopping, or the assistant is on a live call (never tear down a
      runtime mid-call). Returned in ``deferred_names``.

    Returns ``(assistants_to_stop, safe_delete_names, deferred_names)``.
    """
    assistants_to_stop: dict[str, str] = {}
    safe_delete_names: list[str] = []
    deferred_names: list[str] = []
    for job in stale_running:
        job_name = str(job.get("job_name") or "")
        assistant_id = str(job.get("assistant_id") or "")
        if not job_name:
            continue
        if not assistant_id or assistant_id == "unknown":
            safe_delete_names.append(job_name)
            continue

        session_state = session_states.get(assistant_id, {})
        if session_state.get("missing"):
            safe_delete_names.append(job_name)
            continue
        if session_state.get("inspection_failed"):
            deferred_names.append(job_name)
            continue

        bound_job = str(session_state.get("bound_job", "") or "")
        if bound_job != job_name:
            safe_delete_names.append(job_name)
            continue
        if session_state.get("terminal"):
            safe_delete_names.append(job_name)
            continue
        if session_state.get("desired_state") == "Stopped":
            logger.info(
                "[expire_all_stale_jobs] Session %s already stopping stale job %s",
                assistant_id,
                job_name,
            )
            deferred_names.append(job_name)
            continue
        if session_state.get("has_active_call"):
            logger.info(
                "[expire_all_stale_jobs] Session %s has a live call; "
                "deferring stale job %s instead of stopping",
                assistant_id,
                job_name,
            )
            deferred_names.append(job_name)
            continue

        assistants_to_stop[assistant_id] = job_name
        deferred_names.append(job_name)
    return assistants_to_stop, safe_delete_names, deferred_names


def expire_all_stale_jobs(
    max_age_hours: int = 24,
    assistant_id: str | None = None,
) -> dict:
    """Clean up stale K8s Job objects and stop genuinely stale runtimes.

    Stale ``done`` jobs (finished, pod gone) are deleted — their logs are
    preserved in Cloud Logging and GCS independently of the Job object.

    Stale ``running`` jobs (active >max_age_hours) stay session-owned. If a
    stale Job is still the current binding of an AssistantSession, maintenance
    asks Comms to stop the session and lets the controller tear the runtime
    down. A session whose assistant is on a live/pending call is deferred, never
    stopped. Direct Job deletion is reserved for stale ``done`` Jobs and stale
    running Jobs that are no longer the current binding of any session.

    ``assistant_id`` scopes the sweep to a single assistant. Small
    ``max_age_hours`` values (which treat every job as stale) must always be
    paired with a scope so the sweep cannot reclaim unrelated live runtimes.
    """
    admin_key = SETTINGS.orchestra_admin_key
    if not SETTINGS.comms_url:
        return {"total_running": 0, "expired": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    headers = {"Authorization": f"Bearer {admin_key}"}

    try:
        # No ``hours`` lookback: suspended/done Jobs older than a short window
        # must stay visible or they accumulate forever. Staleness is decided
        # below from ``creation_timestamp`` vs ``max_age_hours``.
        resp = requests.get(
            f"{SETTINGS.comms_url}/infra/jobs",
            params={
                # ``idle`` is included so a pool member on a superseded image
                # cannot outlive its usefulness. The controller only ever
                # claims idle Jobs whose image hash is current, so a
                # stale-hash member will never be assigned to anyone -- and
                # nothing else reaps it, because an unassigned pod is exempt
                # from the in-pod idle timer by design (its lifetime belongs
                # to the pool). Two sat in staging for nineteen days.
                "label_selector": "app=unity,unity-status in (running,done,idle)",
            },
            headers=headers,
        )
        if resp.status_code != 200:
            logger.error(
                f"[expire_all_stale_jobs] /infra/jobs returned {resp.status_code}",
            )
            return {"total_running": 0, "expired": 0, "error": resp.text}
    except Exception as e:
        logger.error(f"[expire_all_stale_jobs] /infra/jobs request failed: {e}")
        return {"total_running": 0, "expired": 0, "error": str(e)}

    all_jobs = resp.json().get("jobs", [])

    if assistant_id is not None:
        scope = _sanitize_assistant_label(assistant_id)
        all_jobs = [
            j
            for j in all_jobs
            if _sanitize_assistant_label(str(j.get("assistant_id") or "")) == scope
        ]

    stale = []
    for job in all_jobs:
        ts_str = job.get("creation_timestamp", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                stale.append(job)
        except (ValueError, TypeError):
            continue

    STALE_JOBS_LAST_SWEEP.set(len(stale))

    logger.info(
        f"[expire_all_stale_jobs] Found {len(all_jobs)} running job(s), "
        f"{len(stale)} stale (>{max_age_hours}h old)",
    )

    if not stale:
        return {"total_running": len(all_jobs), "expired": 0}

    # Read once, not per job: the idle branch below compares every candidate
    # against it, and a None (comms unreachable) must leave idle members
    # alone rather than read as "no hash matches, delete them all".
    current_image_hash = _fetch_current_image_hash(headers)

    stale_running = []
    stale_done = []
    for job in stale:
        job_name = job.get("job_name")
        assistant_id = job.get("assistant_id", "unknown")
        unity_status = job.get("labels", {}).get("unity-status", "")
        logger.info(
            "[expire_all_stale_jobs] Stale job: %s assistant_id=%s status=%s created=%s",
            job_name,
            assistant_id,
            unity_status,
            job.get("creation_timestamp"),
        )
        if unity_status == "done":
            stale_done.append(job)
        elif unity_status == "idle":
            # Only the provably unclaimable ones. A current-hash idle member
            # is warm capacity however old it is, and reaping it would just
            # make the pool controller build a replacement; a stale-hash one
            # can never be claimed, so age is beside the point and deleting
            # it is safe. It owns no session and no binding, so it goes the
            # direct route rather than the session-aware one below.
            if (
                current_image_hash
                and job.get("labels", {}).get(
                    _IMAGE_HASH_LABEL,
                )
                != current_image_hash
            ):
                stale_done.append(job)
        else:
            stale_running.append(job)

    cleaned_jobs: list[str] = []

    def _delete_stale_job(job_name: str):
        """Delete a stale Job object.

        Logs are preserved in Cloud Logging (GKE) and GCS (Unity upload).
        The Job object itself is only K8s metadata — deleting it frees
        API-server resources and ensures the job doesn't reappear in the
        next sweep.
        """
        try:
            resp = requests.delete(
                f"{SETTINGS.comms_url}/infra/job/delete",
                data={"job_name": job_name},
                headers=headers,
                timeout=10,
            )
            if resp.status_code in (200, 404):
                return job_name
        except Exception as exc:
            logger.info(
                "[expire_all_stale_jobs] Delete non-fatal for %s: %s",
                job_name,
                exc,
            )
        return None

    if stale_done:
        done_names = [j["job_name"] for j in stale_done if j.get("job_name")]
        logger.info(
            "[expire_all_stale_jobs] Deleting %d stale done jobs",
            len(done_names),
        )
        with ThreadPoolExecutor(max_workers=max(len(done_names), 1)) as executor:
            results = list(executor.map(_delete_stale_job, done_names))
        cleaned_jobs.extend(r for r in results if r is not None)

    stopped_assistants: list[str] = []
    deferred_jobs: list[str] = []

    if stale_running:
        stale_aids = list(
            dict.fromkeys(
                str(j.get("assistant_id"))
                for j in stale_running
                if j.get("assistant_id") and j.get("assistant_id") != "unknown"
            ),
        )

        def _read_session_state(aid: str):
            """Read the current binding and lifecycle state for one session."""

            try:
                session_resp = requests.get(
                    f"{SETTINGS.comms_url}/infra/session/{aid}",
                    headers=headers,
                    timeout=10,
                )
                if session_resp.status_code == 404:
                    return aid, {"missing": True}
                if session_resp.status_code != 200:
                    logger.info(
                        "[expire_all_stale_jobs] Session read failed for %s: %s",
                        aid,
                        session_resp.status_code,
                    )
                    return aid, {"inspection_failed": True}
                session = session_resp.json()
                phase = str(((session.get("status") or {}).get("phase", "")) or "")
                return aid, {
                    "bound_job": (
                        ((session.get("status") or {}).get("binding") or {})
                        .get("jobRef", {})
                        .get("name", "")
                    ),
                    "desired_state": str(
                        ((session.get("spec") or {}).get("desiredState", "")) or "",
                    )
                    or "Running",
                    "terminal": phase in {"Released", "Failed"},
                    "has_active_call": assistant_has_active_call(aid),
                }
            except Exception as exc:
                logger.info(
                    "[expire_all_stale_jobs] Session read non-fatal for %s: %s",
                    aid,
                    exc,
                )
                return aid, {"inspection_failed": True}

        session_states: dict[str, dict] = {}
        if stale_aids:
            with ThreadPoolExecutor(max_workers=len(stale_aids)) as executor:
                session_states = dict(executor.map(_read_session_state, stale_aids))

        (
            assistants_to_stop,
            safe_delete_running_names,
            classified_deferred,
        ) = classify_stale_running(stale_running, session_states)
        deferred_jobs.extend(classified_deferred)

        def _stop_bound_session(item: tuple[str, str]):
            """Ask Comms to stop the session that still owns a stale job."""

            aid, job_name = item
            try:
                resp = requests.post(
                    f"{SETTINGS.comms_url}/infra/session/{aid}/stop",
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    logger.info(
                        "[expire_all_stale_jobs] Stop accepted for session %s "
                        "(bound to stale job %s)",
                        aid,
                        job_name,
                    )
                    return aid
            except Exception as exc:
                logger.info(
                    "[expire_all_stale_jobs] Session stop non-fatal for %s: %s",
                    aid,
                    exc,
                )
            return None

        if assistants_to_stop:
            with ThreadPoolExecutor(max_workers=len(assistants_to_stop)) as executor:
                results = list(
                    executor.map(_stop_bound_session, assistants_to_stop.items()),
                )
            stopped_assistants = [r for r in results if r is not None]

        if safe_delete_running_names:
            logger.info(
                "[expire_all_stale_jobs] Deleting %d stale running jobs "
                "that are no longer session-owned",
                len(safe_delete_running_names),
            )
            with ThreadPoolExecutor(
                max_workers=max(len(safe_delete_running_names), 1),
            ) as executor:
                results = list(
                    executor.map(_delete_stale_job, safe_delete_running_names),
                )
            cleaned_jobs.extend(r for r in results if r is not None)

    logger.info(
        "[expire_all_stale_jobs] Summary: cleaned=%d stale jobs "
        "(%d done + %d running), stopped=%d sessions, deferred=%d jobs",
        len(cleaned_jobs),
        len(stale_done),
        len(stale_running),
        len(stopped_assistants),
        len(deferred_jobs),
    )

    return {
        "total_running": len(all_jobs),
        "expired": len(stale),
        "cleaned_jobs": cleaned_jobs,
        "stopped_assistants": stopped_assistants,
        "deferred_jobs": deferred_jobs,
    }


def _build_start_job_request_data(
    assistant: dict,
    medium: str,
    *,
    wake_reasons: list[dict] | None = None,
    desktop_required: bool | None = None,
) -> dict[str, str]:
    """Build the `/infra/job/start` form payload for one activation request."""

    api_key = assistant["api_key"]
    assistant_id = assistant["assistant_id"]
    desktop_mode = _resolve_desktop_mode(assistant)
    user_desktops = assistant.get("user_desktops", [])
    is_coordinator = assistant.get("is_coordinator", False)
    is_multiplayer = assistant.get("is_multiplayer", False)
    voice_provider, voice_id = resolve_runtime_voice(
        is_coordinator=is_coordinator,
        voice_provider=assistant.get("voice_provider"),
        voice_id=assistant.get("voice_id"),
    )
    data = {
        "api_key": _runtime_str(api_key),
        "medium": _runtime_str(medium),
        "assistant_id": _runtime_str(assistant_id),
        "user_id": _runtime_str(assistant["user_id"]),
        "user_first_name": _runtime_str(assistant["user_first_name"]),
        "user_surname": _runtime_str(assistant["user_surname"]),
        "user_email": _runtime_str(assistant["user_email"]),
        "assistant_first_name": _runtime_str(assistant["assistant_first_name"]),
        "assistant_surname": _runtime_str(assistant["assistant_surname"]),
        "assistant_age": _runtime_str(assistant["assistant_age"]),
        "assistant_nationality": _runtime_str(assistant["assistant_nationality"]),
        "assistant_about": _runtime_str(assistant["assistant_about"]),
        "assistant_job_title": _runtime_str(assistant.get("assistant_job_title")),
        "assistant_timezone": _runtime_str(assistant["assistant_timezone"]),
        "user_number": _runtime_str(assistant["user_number"]),
        "assistant_number": _runtime_str(assistant["assistant_number"]),
        "assistant_email": _runtime_str(assistant["assistant_email"]),
        "assistant_email_provider": _runtime_str(
            assistant.get("assistant_email_provider") or "google_workspace",
        ),
        "user_whatsapp_number": _runtime_str(assistant["user_whatsapp_number"]),
        "assistant_whatsapp_number": _runtime_str(
            assistant.get("assistant_whatsapp_number"),
        ),
        "assistant_discord_bot_id": _runtime_str(
            assistant.get("assistant_discord_bot_id"),
        ),
        "assistant_slack_bot_user_id": _runtime_str(
            assistant.get("assistant_slack_bot_user_id"),
        ),
        "assistant_slack_team_id": _runtime_str(
            assistant.get("assistant_slack_team_id"),
        ),
        "voice_provider": _runtime_str(voice_provider),
        "voice_id": _runtime_str(voice_id),
        "default_model": _runtime_str(assistant.get("default_model")),
        "default_reasoning_effort": _runtime_str(
            assistant.get("default_reasoning_effort"),
        ),
        "slow_brain_model": _runtime_str(assistant.get("slow_brain_model")),
        "slow_brain_reasoning_effort": _runtime_str(
            assistant.get("slow_brain_reasoning_effort"),
        ),
        "desktop_mode": desktop_mode,
        "user_desktops": json.dumps(user_desktops),
        "is_coordinator": ("true" if is_coordinator else "false"),
        "is_multiplayer": ("true" if is_multiplayer else "false"),
        "team_ids": json.dumps(assistant.get("team_ids", [])),
        "team_summaries": encode_team_summaries_for_form(
            assistant.get("team_summaries") or [],
            field_name="team_summaries",
        ),
        # Team-owned assistants have no personal root: the runtime routes
        # shared-scoped tables to Teams/{owner}/… via this value. Dropping it
        # makes the bootstrap Secret deliver null and the live session falls
        # back to deriving ownership from the platform record alone.
        "owner_team_id": (
            str(assistant["owner_team_id"])
            if assistant.get("owner_team_id") is not None
            else ""
        ),
        "self_contact_id": str(_required_contact_id(assistant, "self_contact_id")),
        "boss_contact_id": str(_required_contact_id(assistant, "boss_contact_id")),
        "org_id": (
            str(assistant.get("org_id", ""))
            if assistant.get("org_id") is not None
            else ""
        ),
    }
    if wake_reasons:
        data["wake_reasons"] = json.dumps(wake_reasons)
    if desktop_required is not None:
        data["desktop_required"] = "true" if desktop_required else "false"
    return data


VOICE_CALL_ACTIVATION_CHANNELS = frozenset(
    {"phone", "whatsapp_call", "unify_meet"},
)


def call_activation_defers_desktop_binding(
    channel: str,
    assistant: dict | None = None,
) -> bool:
    """Return whether a voice-call wake can defer managed desktop binding.

    Phone, WhatsApp voice, and Unify Meet do not need the Ubuntu/Windows VM on
    the activation critical path. Desktop binding is promoted once the voice
    agent is ready to speak.
    """
    if channel not in VOICE_CALL_ACTIVATION_CHANNELS:
        return False
    desktop_mode = _resolve_desktop_mode(assistant or {})
    return desktop_mode in ("ubuntu", "windows")


def dispatch_unity_start_intent(
    assistant: dict,
    medium: str,
    *,
    wake_reasons: list[dict] | None = None,
    desktop_required: bool | None = None,
    timeout_seconds: float = START_INTENT_DISPATCH_TIMEOUT_SECONDS,
) -> requests.Response | None:
    """Dispatch `/infra/job/start` and return the observed edge response."""

    api_key = assistant["api_key"]
    assistant_id = assistant["assistant_id"]
    if api_key == "":
        logger.info(f"No user name for assistant {assistant_id}")
        return None

    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}
    return requests.post(
        f"{SETTINGS.comms_url}/infra/job/start",
        headers=headers,
        data=_build_start_job_request_data(
            assistant,
            medium,
            wake_reasons=wake_reasons,
            desktop_required=desktop_required,
        ),
        timeout=timeout_seconds,
    )


def start_unity_job(
    assistant: dict,
    medium: str,
    *,
    desktop_required: bool | None = None,
) -> None:
    """Best-effort low-latency dispatch of activation intent to comms.

    Adapters intentionally stop waiting after a tiny edge timeout so webhook
    and call handlers do not block on AssistantSession convergence. A timeout
    here means "handoff outcome unknown"; callers must not treat this helper as
    proof that comms accepted the request, created a session, or made runtime
    ready.
    """
    assistant_id = assistant["assistant_id"]

    # This is intentionally a fast edge handoff. Adapters does not wait for the
    # full /infra/job/start convergence path to complete on the webhook thread.
    try:
        response = dispatch_unity_start_intent(
            assistant,
            medium,
            desktop_required=desktop_required,
            timeout_seconds=START_INTENT_DISPATCH_TIMEOUT_SECONDS,
        )
        if response is None:
            return
        if response.status_code == 200:
            logger.info(
                f"Activation request accepted by comms for assistant {assistant_id}",
            )
        elif response.status_code == 202:
            logger.info(
                f"Activation request queued for assistant {assistant_id} (pool exhausted)",
            )
        else:
            logger.warning(
                f"Activation request failed for assistant {assistant_id}: "
                f"{response.status_code} {response.text}",
            )
    except requests.exceptions.Timeout:
        logger.info(
            "Activation request client timeout after %sms for assistant %s; "
            "adapters intentionally stop waiting here to preserve webhook "
            "latency. This does not confirm comms accepted the request.",
            int(START_INTENT_DISPATCH_TIMEOUT_SECONDS * 1000),
            assistant_id,
        )
    except requests.RequestException as e:
        logger.error(
            "Activation request failed before adapters observed comms "
            "acceptance for assistant %s: %s",
            assistant_id,
            e,
        )


def uses_local_unity_runtime(assistant_data: dict) -> bool:
    """Return whether inbound traffic should use a caller-local Unity runtime."""

    return bool(assistant_data.get("is_local", False))


class IdlePoolTarget:
    __slots__ = ("target", "min_floor", "demand_buffer")

    def __init__(self, target: int, min_floor: int, demand_buffer: int):
        self.target = target
        self.min_floor = min_floor
        self.demand_buffer = demand_buffer

    @property
    def demand_exceeds_floor(self) -> bool:
        return self.demand_buffer > self.min_floor


def get_target_idle_count(running_count: int) -> IdlePoolTarget:
    """Calculate the target number of idle jobs based on current demand.

    Returns an IdlePoolTarget with:
    - target: max(min_floor, demand_buffer)
    - min_floor: the UNITY_MIN_IDLE_JOBS value
    - demand_buffer: ceil(running_count / UNITY_IDLE_JOB_DEMAND_FACTOR)
    - demand_exceeds_floor: whether demand-based scaling has kicked in
    """
    min_floor = int(os.getenv("UNITY_MIN_IDLE_JOBS", "3"))
    demand_factor = int(os.getenv("UNITY_IDLE_JOB_DEMAND_FACTOR", "5"))

    if demand_factor <= 0:
        return IdlePoolTarget(min_floor, min_floor, 0)

    demand_buffer = -(-running_count // demand_factor)
    return IdlePoolTarget(max(min_floor, demand_buffer), min_floor, demand_buffer)


def _fetch_infra_jobs(
    params: dict,
    *,
    retries: int = 2,
    backoff: float = 0.5,
    caller: str = "",
) -> requests.Response | None:
    """GET /infra/jobs with retries and exponential backoff.

    Returns the Response on 200, or None after all attempts are exhausted.
    """
    tag = f"[{caller}] " if caller else ""
    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}

    for attempt in range(1 + retries):
        try:
            resp = requests.get(
                f"{SETTINGS.comms_url}/infra/jobs",
                params=params,
                headers=headers,
            )
            if resp.status_code == 200:
                return resp
            logger.warning(
                f"{tag}/infra/jobs returned {resp.status_code} "
                f"(attempt {attempt + 1}/{1 + retries})",
            )
        except Exception as e:
            logger.error(
                f"{tag}/infra/jobs error (attempt {attempt + 1}/{1 + retries}): {e}",
            )
        if attempt < retries:
            time.sleep(backoff * (2**attempt))

    return None


def _job_inventory_params(label_selector: str) -> dict[str, str | int]:
    """Build explicit /infra/jobs params for cleanup-sensitive job inventory calls.

    The comms endpoint now returns all matching jobs by default. Adapters still
    want a bounded inventory window so pool maintenance sees recent running and
    idle jobs without silently dropping cross-day jobs during low traffic.
    """
    return {
        "label_selector": label_selector,
        "hours": SETTINGS.job_inventory_lookback_hours,
    }


def get_unity_jobs_inventory() -> dict[str, list[dict]]:
    """Get a categorized inventory of Unity jobs from GKE in a single request.

    Returns:
        A dict with 'running' and 'idle' keys, each containing a list of job dicts
        filtered by the current environment (staging vs production).
    """
    resp = _fetch_infra_jobs(
        _job_inventory_params("app=unity,unity-status!=done"),
        caller="get_unity_jobs_inventory",
    )
    if resp is None:
        return {"running": [], "idle": []}

    all_jobs = resp.json().get("jobs", [])
    inventory: dict[str, list[dict]] = {"running": [], "idle": []}

    for job in all_jobs:
        labels = job.get("labels", {})
        unity_status = labels.get("unity-status")

        if unity_status in ("running", "starting"):
            inventory["running"].append(job)
        elif unity_status == "idle":
            inventory["idle"].append(job)

    return inventory


_IMAGE_HASH_LABEL = "unity-image-hash"


def _idle_job_matches_env(job_name: str) -> bool:
    if SETTINGS.env_suffix:
        return job_name.endswith(SETTINGS.env_suffix)
    return not job_name.endswith("-staging")


def _fetch_current_image_hash(headers: dict | None = None) -> str | None:
    """Return the canonical overlay hash from comms (same source as job creation)."""
    request_headers = headers or {
        "Authorization": f"Bearer {SETTINGS.orchestra_admin_key}",
    }
    try:
        response = requests.get(
            f"{SETTINGS.comms_url}/infra/image",
            headers=request_headers,
            timeout=30,
        )
        response.raise_for_status()
        commit_hash = response.json().get("commit_hash")
        return commit_hash.strip() if commit_hash else None
    except Exception as exc:
        logger.warning("Failed to fetch current image hash: %s", exc)
        return None


def _partition_idle_by_hash(
    idle_jobs: list[dict],
    current_hash: str | None,
) -> tuple[list[dict], list[dict]]:
    if not current_hash:
        return idle_jobs, []

    matching: list[dict] = []
    stale: list[dict] = []
    for job in idle_jobs:
        job_hash = job.get("labels", {}).get(_IMAGE_HASH_LABEL)
        if job_hash == current_hash:
            matching.append(job)
        else:
            stale.append(job)
    return matching, stale


def replenish_idle_pool(refresh: bool = False, extra_demand: int = 0) -> dict:
    """Core logic for idle job pool replenishment.

    Called by `/scheduled/jobs/create` and by `/scheduled/infra/maintenance`.

    Every regime gap-fills to ``effective_target`` using current-image-hash
    idle inventory (``matching_idle_count``), so the warm pool converges to
    ``max(min_floor, ceil(running / demand_factor))`` rather than overshooting:

    - Refresh (``refresh=True``): ensure ``target`` jobs on the current image
      hash after an image bump; does not blindly spawn a full new batch on top
      of existing matching idle. Stale-hash leftovers are removed by cleanup.
    - Floor / demand regimes: create only the shortfall to target.
    - Reactive regime (``extra_demand > 0``): satisfy blocked ``PendingJob``
      sessions while still respecting the same matching-hash gap-fill.
    """
    inventory = get_unity_jobs_inventory()
    running_count = len(inventory["running"])
    idle_jobs = inventory["idle"]
    current_idle_count = len(idle_jobs)
    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}
    current_hash = _fetch_current_image_hash(headers)
    matching_idle_jobs, stale_idle_jobs = _partition_idle_by_hash(
        idle_jobs,
        current_hash,
    )
    matching_idle_count = len(matching_idle_jobs)
    stale_idle_count = len(stale_idle_jobs)
    UNITY_JOBS_RUNNING.set(running_count)
    UNITY_JOBS_IDLE.set(current_idle_count)

    extra_demand = max(0, int(extra_demand))
    pool_target = get_target_idle_count(running_count)
    effective_target = max(pool_target.target, extra_demand)
    num_to_create = max(0, effective_target - matching_idle_count)

    pool_counts = {
        "current": current_idle_count,
        "matching": matching_idle_count,
        "stale": stale_idle_count,
    }

    if num_to_create == 0:
        UNITY_JOBS_RUNNING.set(running_count)
        UNITY_JOBS_IDLE.set(current_idle_count)
        logger.info(
            "Idle pool is healthy "
            f"(current: {current_idle_count}, matching: {matching_idle_count}, "
            f"stale: {stale_idle_count}, target: {effective_target}, "
            f"extra_demand: {extra_demand}). No jobs created.",
        )
        return {
            "status": "healthy",
            "target": effective_target,
            "extra_demand": extra_demand,
            **pool_counts,
        }

    mode = (
        "refresh"
        if refresh
        else (
            "fill-reactive"
            if extra_demand > 0
            else (
                "fill-floor" if not pool_target.demand_exceeds_floor else "fill-demand"
            )
        )
    )
    logger.info(
        f"[{mode}] Creating {num_to_create} idle jobs "
        f"(current: {current_idle_count}, matching: {matching_idle_count}, "
        f"stale: {stale_idle_count}, target: {effective_target}, "
        f"extra_demand: {extra_demand})...",
    )
    if current_hash is None:
        response = requests.get(f"{SETTINGS.comms_url}/infra/image", headers=headers)
        current_hash = response.json()["commit_hash"]
    image = f"{SETTINGS.image_registry}/{SETTINGS.unity_image_name}:{current_hash}"

    def _create_single_job():
        try:
            resp = requests.post(
                f"{SETTINGS.comms_url}/infra/job/create",
                data={"image": image},
                headers=headers,
                timeout=0.1,
            )
            return resp.json()
        except requests.exceptions.Timeout:
            return {"status": "dispatched"}

    with ThreadPoolExecutor(max_workers=num_to_create) as pool:
        futures = [pool.submit(_create_single_job) for _ in range(num_to_create)]
        created_jobs = [f.result() for f in as_completed(futures)]

    UNITY_JOBS_RUNNING.set(running_count)
    UNITY_JOBS_IDLE.set(current_idle_count + len(created_jobs))

    return {
        "mode": mode,
        "created": len(created_jobs),
        "target": effective_target,
        "extra_demand": extra_demand,
        **pool_counts,
        "details": created_jobs,
    }


def _delete_idle_jobs(
    idle_jobs: dict[str, str | None],
    headers: dict,
) -> None:
    def _delete_single_job(job_name, resource_version):
        data = {"job_name": job_name}
        if resource_version is not None:
            data["resource_version"] = resource_version
        resp = requests.delete(
            f"{SETTINGS.comms_url}/infra/job/delete",
            data=data,
            headers=headers,
        )
        if resp.status_code == 409:
            logger.info(
                f"Skipped deleting {job_name}: job changed since listing (409 Conflict)",
            )
        elif resp.status_code != 200:
            logger.warning(
                f"Failed to delete {job_name}: {resp.status_code} {resp.text}",
            )

    if not idle_jobs:
        return

    with ThreadPoolExecutor(max_workers=len(idle_jobs)) as pool:
        futures = [
            pool.submit(_delete_single_job, name, rv) for name, rv in idle_jobs.items()
        ]
        for future in as_completed(futures):
            future.result()


def cleanup_idle_pool() -> dict:
    """Core logic for idle job pool cleanup.

    Called by /scheduled/infra/maintenance.
    """
    headers = {"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"}

    inventory = get_unity_jobs_inventory()
    running_count = len(inventory["running"])
    idle_count = len(inventory["idle"])
    UNITY_JOBS_RUNNING.set(running_count)
    UNITY_JOBS_IDLE.set(idle_count)
    target_retain = get_target_idle_count(running_count).target

    resp = requests.get(
        f"{SETTINGS.comms_url}/infra/jobs",
        params=_job_inventory_params("app=unity,unity-status=idle"),
        headers=headers,
    )
    jobs = resp.json()
    env_idle_jobs = [
        job for job in jobs["jobs"] if _idle_job_matches_env(job["job_name"])
    ]

    current_hash = _fetch_current_image_hash(headers)
    matching_jobs, stale_jobs = _partition_idle_by_hash(env_idle_jobs, current_hash)
    stale_to_delete = {
        job["job_name"]: job.get("resource_version") for job in stale_jobs
    }
    if stale_to_delete:
        logger.info(
            "Cleanup: deleting %s stale-hash idle jobs (current hash: %s)",
            len(stale_to_delete),
            current_hash or "unknown",
        )
        _delete_idle_jobs(stale_to_delete, headers)

    idle_jobs = {job["job_name"]: job.get("resource_version") for job in matching_jobs}

    # Prefer newer idle jobs, but never retain more than target_retain total.
    # (Previously very-new jobs were exempt from the quota, which stacked with
    # hourly refresh creates to ~2x target.)
    very_new_idle_jobs = []  # < 1 min
    new_idle_jobs = []  # 1–11 min
    old_idle_jobs = []  # >= 11 min
    now = datetime.now(timezone.utc)
    for job_name in idle_jobs:
        # job_name format: unity-{YYYY-MM-DD-HH-MM-SS}-{random_id}{-staging}
        job_timestamp_str = "-".join(
            filter(
                lambda part: part.isdigit() and len(part) in [2, 4],
                job_name.split("-"),
            ),
        )
        job_timestamp = datetime.strptime(
            job_timestamp_str,
            "%Y-%m-%d-%H-%M-%S",
        ).replace(tzinfo=timezone.utc)
        delta = now - job_timestamp
        if delta < timedelta(minutes=1):
            very_new_idle_jobs.append(job_name)
        elif delta < timedelta(minutes=11):
            new_idle_jobs.append(job_name)
        else:
            old_idle_jobs.append(job_name)

    candidates = (
        sorted(very_new_idle_jobs, reverse=True)
        + sorted(new_idle_jobs, reverse=True)
        + sorted(old_idle_jobs, reverse=True)
    )
    retain = candidates[:target_retain]
    retain_set = set(retain)

    to_delete = {j: idle_jobs[j] for j in idle_jobs if j not in retain_set}
    logger.info(
        f"Cleanup: retain={len(retain)} "
        f"(very_new={len(very_new_idle_jobs)}, new={len(new_idle_jobs)}, "
        f"old={len(old_idle_jobs)}, target={target_retain}), "
        f"delete_quota={len(to_delete)}, "
        f"delete_stale_hash={len(stale_to_delete)}, running={running_count}",
    )
    logger.info(f"Idle jobs to retain: {sorted(retain)}")
    logger.info(f"Idle jobs to delete: {sorted(to_delete)}")

    _delete_idle_jobs(to_delete, headers)

    UNITY_JOBS_RUNNING.set(running_count)
    UNITY_JOBS_IDLE.set(len(retain))

    return {
        "retained": len(retain),
        "deleted": len(to_delete) + len(stale_to_delete),
        "deleted_stale_hash": len(stale_to_delete),
        "deleted_quota": len(to_delete),
        "target": target_retain,
        "running": running_count,
    }


def _resolve_contacts(
    validate_contact: bool,
    sender: str,
    is_email: bool,
    normalized_sender: str,
    channel: str,
    user_id: str,
    assistant_id: str,
    api_key: str,
    user_number: str,
    user_whatsapp_number: str,
    user_email: str,
    assistant_data: dict,
) -> tuple[list, bool, dict | None]:
    """Resolve contacts for an assistant.

    Returns ``(contacts, is_valid_contact, matched_contact)``.
    """
    if validate_contact:
        return check_valid_contact(
            email_address=(sender if is_email else ""),
            phone_number=("" if is_email else normalized_sender),
            medium=channel,
            assistant_context=f"{user_id}/{assistant_id}",
            api_key=api_key,
            user_number=user_number,
            user_whatsapp_number=user_whatsapp_number,
            user_email=user_email,
            assistant_data=assistant_data,
        )
    response, status_code = get_contacts(
        f"{user_id}/{assistant_id}/Contacts",
        api_key,
    )
    # len(resp_contacts) < 2 handles the race condition on hiring:
    # the contact manager gets initialized in unity so there's a stage
    # where the context is created but contacts haven't been added yet
    logger.info(f"response status_code: {status_code}, contacts: {response}")
    resp_contacts = response["logs"] if status_code == 200 else []
    if len(resp_contacts) < 2:
        logger.info("contact fetching failed, using default contacts")
        return get_default_contacts(assistant_data), True, None
    return [c["entries"] for c in resp_contacts], True, None


def build_webhook_context(
    channel: str,
    destination: str,
    sender: str,
    assistant_id: str = None,
    validate_contact: bool = True,
    ensure_job: bool = True,
    force_start: bool = False,
    assistant_data: dict = None,
    desktop_required: bool | None = None,
):
    """Build a shared context for webhooks.

    Args:
        assistant_data: Optional pre-fetched assistant data to avoid duplicate Orchestra calls.

    Returns legacy ``job_started`` / ``is_job_running`` flags for northbound
    callers. These booleans are compatibility shims: they only mean adapters
    scheduled best-effort dispatch of ``/infra/job/start`` onto the webhook
    background pool. They do not mean adapters observed a comms 200/202, that
    an AssistantSession exists, or that the runtime is ready.
    """
    _t0 = time.perf_counter()
    _ctx_status = "error"
    # normalize identifiers and resolve assistant by channel
    is_email = channel in ["email", "teams"]
    normalized_sender = (
        sender.replace("whatsapp:", "")
        if channel in ("whatsapp", "whatsapp_call")
        else sender
    ).strip()

    requested_assistant_id = assistant_id

    # get assistant data (skip if pre-fetched)
    if assistant_data is None:
        if assistant_id:
            assistant_data = get_assistant(assistant_id=assistant_id)
        else:
            logger.info(
                f"Getting assistant data for {destination} with is_email: {is_email}",
            )
            assistant_data = (
                get_assistant(email_address=destination)
                if is_email
                else get_assistant(phone_number=destination)
            )
    api_key = assistant_data["api_key"]
    assistant_id = assistant_data["assistant_id"] or requested_assistant_id or ""
    assistant_data["assistant_id"] = assistant_id
    user_id = assistant_data["user_id"]
    user_number = assistant_data["user_number"]
    user_whatsapp_number = assistant_data["user_whatsapp_number"]
    user_email = assistant_data["user_email"]
    logger.info(f"assistant_data: {assistant_data}")

    # Resolve contacts first; activation dispatch happens later if startup
    # should proceed for this webhook.
    logger.info(f"validate_contact: {validate_contact}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        contacts_future = pool.submit(
            _resolve_contacts,
            validate_contact,
            sender,
            is_email,
            normalized_sender,
            channel,
            user_id,
            assistant_id,
            api_key,
            user_number,
            user_whatsapp_number,
            user_email,
            assistant_data,
        )
    contacts, is_valid_contact, matched_contact = contacts_future.result()
    logger.info(f"contacts: {contacts}")

    # check contact validity
    is_local_assistant = uses_local_unity_runtime(assistant_data)
    is_test_assistant = "test" in assistant_id
    is_valid_contact = is_valid_contact or is_local_assistant

    # Submit activation intent if needed. The /infra/job/start endpoint handles
    # deduplication atomically and owns the durable convergence path plus the
    # canonical idle-pool top-up. The legacy flags below only mean "dispatch
    # was scheduled on the adapter side", not "runtime is running".
    activation_intent_scheduled = False
    legacy_is_job_running = False
    skip_auto_start = is_test_assistant or is_local_assistant
    should_start_job = (
        ensure_job and is_valid_contact and (force_start or not skip_auto_start)
    )
    effective_desktop_required = desktop_required
    if effective_desktop_required is None and call_activation_defers_desktop_binding(
        channel,
        assistant_data,
    ):
        effective_desktop_required = False
    if effective_desktop_required is None:
        effective_desktop_required = managed_desktop_entitled(assistant_data)
    if should_start_job:
        JOB_DEMAND_TOTAL.labels(channel=channel).inc()
        _WEBHOOK_BG_POOL.submit(
            partial(
                start_unity_job,
                assistant_data,
                channel,
                desktop_required=effective_desktop_required,
            ),
        )
        activation_intent_scheduled = True
        legacy_is_job_running = True

    logger.info(f"is_valid_contact: {is_valid_contact}")
    _ctx_status = "error" if assistant_data.get("assistant_id") is None else "success"
    BUILD_WEBHOOK_CONTEXT_DURATION.labels(
        channel=channel,
        job_started=str(activation_intent_scheduled).lower(),
        status=_ctx_status,
    ).observe(time.perf_counter() - _t0)
    return {
        "assistant": assistant_data,
        "contacts": contacts,
        "is_valid_contact": is_valid_contact,
        "matched_contact": matched_contact,
        "is_job_running": legacy_is_job_running,
        "job_started": activation_intent_scheduled,
    }


# phone helpers
def get_twilio_client():
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)


def get_twilio_wa_client():
    account_sid = os.getenv("TWILIO_WA_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_WA_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError("TWILIO_WA_ACCOUNT_SID and TWILIO_WA_AUTH_TOKEN must be set")
    return TwilioClient(account_sid, auth_token)


_CONFERENCE_RINGBACK_URL = (
    "https://auburn-eagle-6359.twil.io/assets/ring-tone-68676.mp3"
)


def create_conference_response(conference_name, with_status=False, ringback=True):
    """TwiML joining a participant to a named conference.

    The first participant into a Twilio conference hears the ``wait_url``
    audio until a second joins. ``ringback`` stays True only for an inbound
    caller's own leg (a human waiting for us to answer). Legs we dial — the
    LiveKit SIP/agent leg, or a human who answered our call — wait in silence
    so ring audio never plays into the LiveKit room or at someone who already
    picked up.

    ``beep`` defaults to true on Twilio, playing a join tone into the
    conference the moment a participant enters — heard by the callee right as
    they pick up (an artificial "call answered" sound) and by the agent's STT.
    Disabled on every leg.
    """
    resp_user = VoiceResponse()
    dial_user = resp_user.dial()
    wait_url = _CONFERENCE_RINGBACK_URL if ringback else ""
    if with_status:
        dial_user.conference(
            conference_name,
            startConferenceOnEnter=True,
            endConferenceOnExit=True,
            muted=False,
            beep=False,
            wait_url=wait_url,
            status_callback=f"{SETTINGS.comms_url}/phone/conference-status",
            status_callback_event="end",
        )
        return resp_user
    dial_user.conference(
        conference_name,
        startConferenceOnEnter=True,
        endConferenceOnExit=True,
        muted=False,
        beep=False,
        wait_url=wait_url,
    )
    return resp_user


def add_user_to_conference(
    conference_name,
    from_number,
    to_number_uri,
    connect_third_party=False,
):
    twilio_client = get_twilio_client()

    if connect_third_party:
        conferences = twilio_client.conferences.list(
            friendly_name=conference_name,
            status="in-progress",
        )
        participants = twilio_client.conferences(conferences[0].sid).participants.list()
        for participant in participants:
            call = twilio_client.calls(participant.call_sid).fetch()
            # Identify Livekit Agent and mute
            if "livekit.cloud" in call.to:
                twilio_client.conferences(conferences[0].sid).participants(
                    participant.sid,
                ).update(muted=True)
                break
        response = create_conference_response(
            conference_name,
            with_status=True,
            ringback=False,
        )
    else:
        response = create_conference_response(conference_name, ringback=False)

    call = twilio_client.calls.create(
        to=to_number_uri,
        from_=from_number,
        twiml=str(response),
    )
    return call.sid


# email helpers
def _strip_quoted_text(text: str) -> str:
    """Remove quoted text and signatures from email content."""
    lines = text.splitlines()
    cleaned = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(">"):
            continue
        if re.match(r"On .+wrote:", stripped) or stripped.startswith(
            "-----Original Message-----",
        ):
            break
        cleaned.append(line)
    return "\n".join(cleaned).strip()


# =============================================================================
# Outlook Helpers
# =============================================================================


class TokenCredentialFromSecret(TokenCredential):
    """Wraps a stored access token for use with Microsoft Graph SDK."""

    def __init__(self, access_token: str):
        self._token = access_token

    def get_token(self, *scopes, **kwargs) -> AccessToken:
        # Expiry doesn't matter - scheduled job keeps token fresh
        return AccessToken(
            self._token,
            int(datetime.now(tz=timezone.utc).timestamp()) + 3600,
        )


_GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]


def get_graph_client_from_token(access_token: str) -> GraphServiceClient:
    """Create a Graph client from a per-user OAuth access token."""
    return GraphServiceClient(
        credentials=TokenCredentialFromSecret(access_token),
        scopes=_GRAPH_SCOPES,
    )


def get_admin_graph_client() -> GraphServiceClient:
    """Build a Graph client using tenant-level client credentials.

    Used for mailbox operations on provisioned MS365 users that don't
    have per-user OAuth tokens.
    """
    from azure.identity import ClientSecretCredential

    tenant_id = os.getenv("MS365_ADMIN_TENANT_ID", "")
    client_id = os.getenv("MS365_ADMIN_CLIENT_ID", "")
    client_secret = os.getenv("MS365_ADMIN_CLIENT_SECRET", "")
    if not all([tenant_id, client_id, client_secret]):
        raise RuntimeError(
            "MS365 admin credentials not configured "
            "(MS365_ADMIN_TENANT_ID, MS365_ADMIN_CLIENT_ID, MS365_ADMIN_CLIENT_SECRET)",
        )
    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    return GraphServiceClient(credentials=credential, scopes=_GRAPH_SCOPES)


def get_admin_graph_bearer_token() -> str:
    """Acquire an app-only bearer token for Microsoft Graph.

    Uses the same ``MS365_ADMIN_*`` client credentials as
    ``get_admin_graph_client``.  Returned token targets the
    ``https://graph.microsoft.com/.default`` scope and is suitable for
    direct ``httpx`` calls against paths the Graph SDK doesn't expose
    cleanly (e.g. ``/users/{email}/chats/...``).
    """
    from azure.identity import ClientSecretCredential

    tenant_id = os.getenv("MS365_ADMIN_TENANT_ID", "")
    client_id = os.getenv("MS365_ADMIN_CLIENT_ID", "")
    client_secret = os.getenv("MS365_ADMIN_CLIENT_SECRET", "")
    if not all([tenant_id, client_id, client_secret]):
        raise RuntimeError(
            "MS365 admin credentials not configured "
            "(MS365_ADMIN_TENANT_ID, MS365_ADMIN_CLIENT_ID, MS365_ADMIN_CLIENT_SECRET)",
        )
    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    return credential.get_token("https://graph.microsoft.com/.default").token


def get_outlook_graph_client(secrets: dict) -> tuple[GraphServiceClient, bool]:
    """Return a Graph client and whether it uses per-user OAuth.

    Returns:
        (graph_client, has_user_token) — ``has_user_token`` is True when
        using a per-user OAuth token (operations should target ``/me``),
        False when using admin app credentials (operations must target
        ``/users/{email}``).
    """
    access_token = secrets.get("MICROSOFT_ACCESS_TOKEN")
    if access_token:
        return get_graph_client_from_token(access_token), True
    return get_admin_graph_client(), False


async def get_outlook_thread_id(
    email_id: str,
    graph_client,
    *,
    user_email: str | None = None,
):
    """Fetch Outlook message details from a notification.

    When ``user_email`` is provided the message is fetched via
    ``/users/{email}/messages/...`` (app credentials).  Otherwise
    ``/me/messages/...`` is used (delegated token).
    """
    try:
        request_config = (
            MessageItemRequestBuilder.MessageItemRequestBuilderGetRequestConfiguration()
        )
        request_config.headers.add("Prefer", 'outlook.body-content-type="text"')
        request_config.query_parameters = (
            MessageItemRequestBuilder.MessageItemRequestBuilderGetQueryParameters(
                select=[
                    "id",
                    "conversationId",
                    "subject",
                    "body",
                    "uniqueBody",
                    "from",
                    "toRecipients",
                    "ccRecipients",
                    "bccRecipients",
                    "receivedDateTime",
                    "hasAttachments",
                ],
            )
        )

        if user_email:
            user_node = graph_client.users.by_user_id(user_email)
        else:
            user_node = graph_client.me

        message = await user_node.messages.by_message_id(email_id).get(
            request_configuration=request_config,
        )

        if not message:
            logger.info("Message %s not found", email_id)
            return None, None, None

        last_message = {
            "sender": message.from_.email_address.address if message.from_ else "",
            "to": [r.email_address.address for r in (message.to_recipients or [])],
            "cc": [r.email_address.address for r in (message.cc_recipients or [])],
            "bcc": [r.email_address.address for r in (message.bcc_recipients or [])],
            "subject": message.subject or "",
            "content": message.unique_body.content if message.unique_body else "",
            "received_at": (
                message.received_date_time.isoformat()
                if message.received_date_time
                else None
            ),
            "has_attachments": message.has_attachments,
            "attachments": [],
        }

        conversation_id = message.conversation_id
        logger.info(
            "conversation_id: %s, email_id: %s",
            conversation_id,
            email_id,
        )

        return conversation_id, email_id, last_message

    except Exception as e:
        logger.error("Error fetching Outlook message: %s", e, exc_info=True)
        return None, None, None


# =============================================================================
# Gmail Helpers
# =============================================================================


def _header(headers, name: str) -> str:
    """Extract a specific header from email headers."""
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _payload_text(payload) -> str:
    """Extract text content from email payload."""
    mime_type = payload.get("mimeType", "")
    if mime_type.startswith("text/") and payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        decoded = base64.urlsafe_b64decode(data.encode("utf-8"))
        latest = _strip_quoted_text(decoded.decode("utf-8", errors="replace"))
        return latest

    for part in payload.get("parts", []):
        txt = _payload_text(part)
        if txt:
            return txt
    return ""


def _collect_attachments(payload):
    attachments = []
    if not payload:
        return attachments
    body = payload.get("body", {})
    filename = payload.get("filename")
    attachment_id = body.get("attachmentId")
    mime_type = payload.get("mimeType")
    size = body.get("size")
    if attachment_id:
        attachments.append(
            {
                "id": attachment_id,
                "filename": filename or "",
                "mimeType": mime_type,
                "size": size,
            },
        )
    for part in payload.get("parts", []):
        attachments.extend(_collect_attachments(part))
    return attachments


def _gmail_thread_to_conversation(thread):
    """Convert a Gmail thread to a structured conversation."""
    convo = []
    for msg in thread.get("messages", []):
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        convo.append(
            {
                "sender": _header(headers, "From"),
                "to": (
                    [_addr.strip() for _addr in _header(headers, "To").split(",")]
                    if _header(headers, "To")
                    else []
                ),
                "cc": (
                    [_addr.strip() for _addr in _header(headers, "Cc").split(",")]
                    if _header(headers, "Cc")
                    else []
                ),
                "bcc": (
                    [_addr.strip() for _addr in _header(headers, "Bcc").split(",")]
                    if _header(headers, "Bcc")
                    else []
                ),
                "subject": _header(headers, "Subject").replace("Re: ", ""),
                # Original envelope recipient, preserved by the catch-all
                # routing rule. The only surviving copy of a twin alias when
                # the twin was BCC'd (To/Cc never contained it).
                "x_gm_original_to": _header(headers, "X-Gm-Original-To"),
                "content": _payload_text(payload),
            },
        )
    return convo


def get_thread_id(user_id, history_id, gmail_service):
    """Process Gmail history and thread to extract conversation data."""
    try:
        # Get history events for label changes
        histories = (
            gmail_service.users()
            .history()
            .list(
                userId=user_id,
                startHistoryId=history_id,
            )
            .execute()
        )
        print(f"pre-histories: {histories}")

        # Safeguard for thread replies
        if "history" not in histories or not histories["history"]:
            histories["history"] = [
                (
                    gmail_service.users()
                    .messages()
                    .list(
                        userId=user_id,
                        q="is:unread newer_than:1d",
                    )
                    .execute()
                ),
            ]

        # Process each history entry
        print(f"histories: {histories}")
        for history in histories["history"]:
            print(f"history: {history}")
            messages = history.get("messages", [])
            print(f"messages: {messages}")
            if len(messages) == 0:
                continue

            # Get the message details (Gmail message resource id)
            msg_id = messages[-1]["id"]
            message = (
                gmail_service.users()
                .messages()
                .get(userId=user_id, id=msg_id)
                .execute()
            )
            print(f"message: {message} {msg_id}")
            message_headers = message["payload"].get("headers", [])
            print(f"message_headers: {message_headers}")
            # RFC 5322 header names are case-insensitive and Message-ID is
            # technically optional; senders vary (`Message-Id`, `Message-ID`,
            # occasionally absent for bulk mail). Use the shared helper and
            # fall through to None rather than aborting the whole message.
            email_id = _header(message_headers, "Message-ID") or None
            print(f"email_id: {email_id}")

            # Extract attachments from the Gmail message payload
            attachments = _collect_attachments(message.get("payload", {}))
            print(f"attachments: {attachments}")

            labels = message.get("labelIds", [])
            print(f"labels: {labels}")
            if labels and "UNREAD" not in labels:
                print(f"Message {msg_id} is read, skipping")
                continue

            gmail_service.users().messages().modify(
                userId=user_id,
                id=msg_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()

            # Get the thread for this message
            thread_id = message["threadId"]
            thread = (
                gmail_service.users()
                .threads()
                .get(userId=user_id, id=thread_id, format="full")
                .execute()
            )
            print(f"thread: {thread} {thread_id}")

            # Convert to conversation format
            conversation = _gmail_thread_to_conversation(thread)
            print(f"conversation: {conversation}")
            last_message = conversation[-1]
            print(f"last_message: {last_message}")

            # Attach filenames with IDs to the last_message for publishing
            last_message["attachments"] = [
                {"id": att["id"], "filename": att.get("filename", "")}
                for att in attachments
            ]

            # Return the conversation plus Gmail message id
            return thread_id, email_id, last_message, msg_id

        return None, None, None, None

    except Exception as e:
        print(f"Error processing history for user {user_id}: {str(e)}")
        traceback.print_exc()
        return None, None, None, None


def publish_gmail_thread_id(
    assistant_id,
    user_id,
    thread_id,
    email_id,
    last_message,
    contacts,
    gmail_message_id=None,
    shared_mailbox=None,
):
    """Publish the thread_id and user_id to a different pub/sub topic."""
    try:
        publisher = get_pubsub_client()
        topic_name = SETTINGS.assistant_topic(assistant_id)
        topic_path = publisher.topic_path(SETTINGS.gcp_project_id, topic_name)

        message_dict = {
            "thread": "email",
            "publish_timestamp": time.time(),
            "event": {
                "contacts": contacts,
                "thread_id": thread_id,
                "email_id": email_id,
                "gmail_message_id": gmail_message_id,
                "attachments": last_message.get("attachments", []),
                "from": last_message["sender"],
                "to": last_message["to"],
                "cc": last_message["cc"],
                "bcc": last_message["bcc"],
                "subject": last_message["subject"],
                "body": last_message["content"],
            },
        }
        if shared_mailbox:
            message_dict["event"]["shared_mailbox"] = shared_mailbox
        data = json.dumps(message_dict).encode("utf-8")

        # Publish asynchronously
        publish_future = publisher.publish(topic_path, data=data, thread="inbound")
        if "test" in assistant_id:
            pubsub_message_id = publish_future.result(timeout=10)
            print(f"Message ID: {pubsub_message_id}")
        print(f"Published thread_id {thread_id} for user {user_id} to {topic_path}")
    except Exception as e:
        print(f"Failed to publish thread_id {thread_id} for user {user_id}: {e}")


def publish_outlook_thread_id(
    assistant_id,
    user_id,
    conversation_id,
    email_id,
    last_message,
    contacts,
):
    """Publish the Outlook conversation to pub/sub topic."""
    try:
        publisher = get_pubsub_client()
        topic_name = SETTINGS.assistant_topic(assistant_id)
        topic_path = publisher.topic_path(SETTINGS.gcp_project_id, topic_name)

        message_dict = {
            "thread": "email",
            "publish_timestamp": time.time(),
            "event": {
                "contacts": contacts,
                "thread_id": conversation_id,
                "email_id": email_id,
                "attachments": last_message.get("attachments", []),
                "from": last_message["sender"],
                "to": last_message["to"],
                "cc": last_message["cc"],
                "bcc": last_message.get("bcc", ""),
                "subject": last_message["subject"],
                "body": last_message["content"],
            },
        }
        data = json.dumps(message_dict).encode("utf-8")

        publish_future = publisher.publish(topic_path, data=data, thread="inbound")
        if "test" in assistant_id:
            msg_id = publish_future.result(timeout=10)
            logger.info(f"Message ID: {msg_id}")
        logger.info(
            f"Published conversation_id {conversation_id} for user {user_id} to {topic_path}",
        )
    except Exception as e:
        logger.info(
            f"Failed to publish conversation_id {conversation_id} for user {user_id}: {e}",
        )


def dispatch_livekit_agent(room_name: str):
    response = requests.post(
        f"{SETTINGS.comms_url}/phone/dispatch-livekit-agent",
        headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
        json={"room_name": room_name},
    )
    if response.status_code != 200:
        logger.info(f"Failed to dispatch LiveKit agent. Status: {response.status_code}")
        return False
    return True


# OAuth helpers were moved to ``common/microsoft_oauth.py`` and
# ``common/google_oauth.py`` so the comms service can call them
# without depending on this module.  Import them from there.


# ---------------------------------------------------------------------------
# Billing-gate auto-replies
# ---------------------------------------------------------------------------

COMMS_GATE_TIMEOUT_SECONDS = 5


def check_comms_gate(assistant_id: str | int | None) -> dict | None:
    """Billing-gate state for an assistant, or ``None`` when not gated.

    Fail-open by design: a gate-lookup hiccup must never take down inbound
    comms — hard billing enforcement lives server-side in the runtime's
    spending gate; this call only powers the courtesy auto-reply.
    """
    if not assistant_id:
        return None
    try:
        response = requests.get(
            f"{SETTINGS.orchestra_url}/admin/billing/comms-gate",
            params={"assistant_id": assistant_id},
            headers={"Authorization": f"Bearer {SETTINGS.orchestra_admin_key}"},
            timeout=COMMS_GATE_TIMEOUT_SECONDS,
        ).json()
    except Exception as exc:
        logger.warning("comms-gate lookup failed (fail-open): %s", exc)
        return None
    if isinstance(response, dict) and response.get("gated"):
        return response
    return None


def _normalize_phone(number: str | None) -> str:
    return re.sub(r"[^\d+]", "", (number or "").replace("whatsapp:", ""))


def is_owner_sender(assistant_data: dict, channel: str, sender: str) -> bool:
    """Whether the inbound sender is the assistant's own user.

    The billing-gate auto-reply goes only to the account owner — the one
    person who can fix the billing state. Third-party contacts get the
    unchanged behaviour (silence) so a paused account's billing state is
    never disclosed to strangers.
    """
    if channel == "email":
        owner = (assistant_data.get("user_email") or "").strip().lower()
        return bool(owner) and sender.strip().lower() == owner
    sender_norm = _normalize_phone(sender)
    if not sender_norm:
        return False
    owners = {
        _normalize_phone(assistant_data.get("user_number")),
        _normalize_phone(assistant_data.get("user_whatsapp_number")),
    }
    owners.discard("")
    return sender_norm in owners


def send_billing_gate_notice(
    gmail_service,
    *,
    mailbox: str,
    to_email: str,
    original_subject: str,
    message: str,
) -> None:
    """Reply to the owner's inbound email with the billing-gate explanation.

    Owner-only (see ``is_owner_sender``), one notice per inbound message —
    the same loop-safety envelope as ``send_twin_moved_notice``.
    """
    import base64 as _base64
    from email.mime.text import MIMEText as _MIMEText

    msg = _MIMEText(message)
    msg["to"] = to_email
    msg["from"] = mailbox
    subject = (original_subject or "").strip()
    msg["subject"] = f"Re: {subject}" if subject else "Your Unify assistant is paused"
    try:
        gmail_service.users().messages().send(
            userId="me",
            body={"raw": _base64.urlsafe_b64encode(msg.as_bytes()).decode()},
        ).execute()
        logger.info("Sent billing-gate notice over email")
    except Exception as exc:
        logger.error("Failed to send billing-gate notice: %s", exc)


def send_slack_billing_gate_notice(
    team_id: str,
    channel_id: str,
    thread_ts: str | None,
    message: str,
) -> None:
    """Post the billing-gate explanation into the sender's Slack DM.

    Best-effort: a missing bot token or a failed post is logged and
    swallowed — the notice is a courtesy, never a delivery guarantee.
    """
    bot_token = _resolve_slack_bot_token(team_id)
    if not bot_token:
        logger.warning("slack billing-gate notice skipped: no bot token")
        return
    body: dict = {"channel": channel_id, "text": message}
    if thread_ts:
        body["thread_ts"] = thread_ts
    try:
        payload = requests.post(
            f"{SLACK_API_BASE}/chat.postMessage",
            json=body,
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=10,
        ).json()
        if not payload.get("ok"):
            logger.warning(
                "slack billing-gate notice failed: %s",
                payload.get("error"),
            )
    except Exception as exc:
        logger.error("Failed to send Slack billing-gate notice: %s", exc)


def send_ms_teams_billing_gate_notice(activity: dict, message: str) -> None:
    """Reply to a 1:1 Teams bot conversation with the billing-gate notice."""
    _send_ms_teams_bot_message(
        activity,
        None,
        {"type": "message", "text": message},
    )


async def send_outlook_billing_gate_notice(
    graph_client,
    *,
    has_user_token: bool,
    mailbox: str,
    to_email: str,
    original_subject: str,
    message: str,
) -> None:
    """Reply to the owner's inbound Outlook email with the gate explanation.

    Targets ``/me`` for delegated tokens and ``/users/{mailbox}`` for admin
    app credentials, mirroring the read path in ``get_outlook_thread_id``.
    """
    from msgraph.generated.models.body_type import BodyType
    from msgraph.generated.models.email_address import EmailAddress
    from msgraph.generated.models.item_body import ItemBody
    from msgraph.generated.models.message import Message
    from msgraph.generated.models.recipient import Recipient
    from msgraph.generated.users.item.send_mail.send_mail_post_request_body import (
        SendMailPostRequestBody,
    )

    subject = (original_subject or "").strip()
    graph_message = Message(
        subject=f"Re: {subject}" if subject else "Your Unify assistant is paused",
        body=ItemBody(content_type=BodyType.Text, content=message),
        to_recipients=[
            Recipient(email_address=EmailAddress(address=to_email)),
        ],
    )
    request_body = SendMailPostRequestBody(
        message=graph_message,
        save_to_sent_items=True,
    )
    try:
        if has_user_token:
            await graph_client.me.send_mail.post(request_body)
        else:
            await graph_client.users.by_user_id(mailbox).send_mail.post(request_body)
        logger.info("Sent billing-gate notice over Outlook")
    except Exception as exc:
        logger.error("Failed to send Outlook billing-gate notice: %s", exc)
