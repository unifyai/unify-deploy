"""Bridge Google Meet Workspace Events Pub/Sub pushes into Orchestra ingress.

Google delivers Meet Workspace Events (e.g.
``google.workspace.meet.transcript.v2.fileGenerated``) only through Cloud
Pub/Sub, formatted as CloudEvents. A push subscription targets the Adapters
``POST /meet/workspace-events`` endpoint. This module owns the translation from
that Pub/Sub push into a Unify-shaped, HMAC-signed request that Orchestra's
``native_google`` webhook ingress accepts.

Responsibilities, kept out of the request handler so they stay unit-testable:

- verify the Pub/Sub push OIDC token (Google-signed, correct audience and
  service-account email);
- decode the Pub/Sub envelope and its embedded CloudEvent into the native
  delivery fields Orchestra normalize requires (retry-stable ``event_id``,
  ``provider_trigger_slug``, ``external_trigger_id`` = Workspace Events
  subscription name, transcript reference metadata);
- sign the outbound body with the shared ``NATIVE_GOOGLE_WEBHOOK_SECRET`` using
  the same scheme Orchestra verifies (``v1,`` base64 HMAC-SHA256 over
  ``{id}.{timestamp}.{body}``);
- POST it to the unrouted native_google ingress path so a shared topic does not
  require one Pub/Sub subscription per trigger — Orchestra resolves the binding
  by ``external_trigger_id``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import requests
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token as google_id_token

logger = logging.getLogger(__name__)

# Default event type for the first Meet vertical. The bridge forwards whatever
# ``ce-type`` the CloudEvent carries and only falls back to this when the
# attribute is absent, so the ingress stays provider/event neutral.
MEET_TRANSCRIPT_SLUG = "google.workspace.meet.transcript.v2.fileGenerated"

# Workspace Events stamps ``ce-source`` as
# ``//workspaceevents.googleapis.com/subscriptions/<id>``. Stripping this prefix
# yields the ``subscriptions/<id>`` name that live provision persists as the
# binding's ``external_trigger_id``.
_WORKSPACE_EVENTS_SOURCE_PREFIX = "//workspaceevents.googleapis.com/"

# Orchestra's native_google ingress accepts deliveries without an ingress key
# and resolves the binding by ``external_trigger_id`` from the body.
_NATIVE_GOOGLE_INGRESS_SUFFIX = "/webhooks/integrations/native_google"

_ORCHESTRA_POST_TIMEOUT_SECONDS = 30

# Reused across pushes so OIDC verification shares one HTTP connection pool to
# Google's certs endpoint instead of opening a fresh session per delivery.
_OIDC_REQUEST = google_auth_requests.Request()


class MeetPushAuthError(Exception):
    """The Pub/Sub push could not be authenticated as a Google-signed OIDC."""


class MeetBridgeMappingError(Exception):
    """The Pub/Sub envelope was not a mappable Meet Workspace Events push."""


@dataclass(frozen=True)
class NativeMeetDelivery:
    """A Meet CloudEvent mapped to the Orchestra native_google body."""

    event_id: str
    external_trigger_id: str
    payload: dict[str, Any]


def verify_meet_push_oidc(
    bearer_token: str,
    *,
    audience: str,
    service_account_email: str,
) -> Mapping[str, Any]:
    """Verify a Pub/Sub push OIDC token, returning its claims.

    Pub/Sub signs each push with a Google OIDC token minted for the configured
    push service account, using the Adapters base URL as the audience. This
    validates the Google signature, expiry, and audience (via google-auth) and
    additionally pins the token's ``email`` to the expected push identity so a
    forged request from another principal is rejected.

    Raises ``MeetPushAuthError`` on any failure so the caller can reject the
    push without waking a task.
    """

    if not bearer_token:
        raise MeetPushAuthError("missing bearer token on Meet Pub/Sub push")

    # ``verify_oauth2_token`` raises ``ValueError`` for a bad signature, expiry,
    # or audience and ``GoogleAuthError`` for a non-Google issuer (a forged
    # token) or a certs-fetch failure. Treat all of them as an auth failure so
    # the caller rejects the push (401) rather than surfacing a 500; a rejected
    # push wakes no task and Pub/Sub retries transient certs failures.
    try:
        claims = google_id_token.verify_oauth2_token(
            bearer_token,
            _OIDC_REQUEST,
            audience=audience,
        )
    except Exception as exc:
        raise MeetPushAuthError(f"invalid Meet push OIDC token: {exc}") from exc

    if service_account_email:
        token_email = claims.get("email")
        if token_email != service_account_email:
            raise MeetPushAuthError(
                "Meet push OIDC email does not match expected push identity",
            )
        if not claims.get("email_verified", False):
            raise MeetPushAuthError("Meet push OIDC email is not verified")

    return claims


def bearer_token_from_authorization(authorization_header: str | None) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""

    if not authorization_header:
        return ""
    prefix = "bearer "
    if authorization_header[: len(prefix)].lower() != prefix:
        return ""
    return authorization_header[len(prefix) :].strip()


def _decode_cloud_event_data(raw_data: str | None) -> dict[str, Any]:
    """Decode the base64 CloudEvent ``data`` payload into a dict, if present."""

    if not raw_data:
        return {}
    try:
        decoded = base64.b64decode(raw_data).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        logger.warning("Meet bridge: CloudEvent data was not valid base64/UTF-8")
        return {}
    if not decoded.strip():
        return {}
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError:
        logger.warning("Meet bridge: CloudEvent data was not valid JSON")
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _external_trigger_id_from_source(*candidates: str | None) -> str:
    """Return the ``subscriptions/<id>`` name from a Workspace Events source."""

    for candidate in candidates:
        value = (candidate or "").strip()
        if not value:
            continue
        if value.startswith(_WORKSPACE_EVENTS_SOURCE_PREFIX):
            value = value[len(_WORKSPACE_EVENTS_SOURCE_PREFIX) :]
        if value.startswith("subscriptions/"):
            return value
    return ""


def map_pubsub_push_to_delivery(envelope: Mapping[str, Any]) -> NativeMeetDelivery:
    """Map a Pub/Sub push envelope + CloudEvent to the native_google body.

    Google's Workspace Events pushes arrive in CloudEvents binary content mode:
    context attributes live under ``message.attributes`` (``ce-id``,
    ``ce-source``, ``ce-type``, ``ce-time``, ``ce-subject``) and the resource
    reference is a base64 JSON blob under ``message.data``.

    Raises ``MeetBridgeMappingError`` when the push is not a Meet Workspace
    Events delivery or is missing the retry-stable identity / subscription name
    the ingress needs, so the caller rejects it without waking a task.
    """

    if not isinstance(envelope, Mapping):
        raise MeetBridgeMappingError("push body is not a JSON object")
    message = envelope.get("message")
    if not isinstance(message, Mapping):
        raise MeetBridgeMappingError("push body has no Pub/Sub message")

    attributes = message.get("attributes")
    attributes = attributes if isinstance(attributes, Mapping) else {}

    event_id = str(attributes.get("ce-id") or "").strip()
    if not event_id:
        raise MeetBridgeMappingError("Meet CloudEvent is missing ce-id")

    # Workspace Events sets both ``ce-source`` and the Pub/Sub ``orderingKey``
    # to ``//workspaceevents.googleapis.com/subscriptions/<id>``; the latter is
    # a fallback if the source attribute is ever absent.
    external_trigger_id = _external_trigger_id_from_source(
        attributes.get("ce-source"),
        message.get("orderingKey"),
    )
    if not external_trigger_id:
        raise MeetBridgeMappingError(
            "Meet CloudEvent is missing a Workspace Events subscription name",
        )

    slug = str(attributes.get("ce-type") or "").strip() or MEET_TRANSCRIPT_SLUG
    occurred_at = str(attributes.get("ce-time") or "").strip()
    subject = str(attributes.get("ce-subject") or "").strip()

    # Reference metadata only (transcript resource name / file id), never the
    # transcript bytes. ``includeResource=false`` subscriptions still carry the
    # transcript resource name in the CloudEvent data.
    data: dict[str, Any] = dict(_decode_cloud_event_data(message.get("data")))
    if subject and "resource" not in data:
        data["resource"] = subject
    # Orchestra's identity extraction falls back to nested ``data`` fields, so
    # carry the retry-stable id there too without clobbering a provider value.
    data.setdefault("event_id", event_id)

    payload: dict[str, Any] = {
        "event_id": event_id,
        "provider_trigger_slug": slug,
        "external_trigger_id": external_trigger_id,
        "data": data,
    }
    if occurred_at:
        payload["occurred_at"] = occurred_at

    return NativeMeetDelivery(
        event_id=event_id,
        external_trigger_id=external_trigger_id,
        payload=payload,
    )


def sign_native_webhook_headers(
    *,
    signing_secret: str,
    raw_body: bytes,
    webhook_id: str,
) -> dict[str, str]:
    """Build native trigger delivery headers for one body.

    Mirrors Orchestra's signing scheme exactly: HMAC-SHA256 over
    ``{webhook_id}.{timestamp}.{body}`` with the shared secret, base64 encoded
    and prefixed ``v1,``. Orchestra rejects timestamps outside a 300s window, so
    the timestamp is stamped here immediately before sending.
    """

    webhook_timestamp = str(int(time.time()))
    body_text = raw_body.decode("utf-8")
    digest = base64.b64encode(
        hmac.new(
            signing_secret.encode("utf-8"),
            f"{webhook_id}.{webhook_timestamp}.{body_text}".encode("utf-8"),
            hashlib.sha256,
        ).digest(),
    ).decode("utf-8")
    return {
        "x-unify-webhook-id": webhook_id,
        "x-unify-webhook-timestamp": webhook_timestamp,
        "x-unify-webhook-signature": f"v1,{digest}",
    }


def post_native_google_delivery(
    delivery: NativeMeetDelivery,
    *,
    orchestra_url: str,
    signing_secret: str,
) -> requests.Response:
    """Sign and POST a mapped delivery to Orchestra's native_google ingress.

    Uses the unrouted ingress path (no ingress key); Orchestra resolves the
    binding from ``external_trigger_id`` in the body. Signs the exact compact
    JSON bytes that are sent so the HMAC matches on the verify side.
    """

    raw_body = json.dumps(delivery.payload, separators=(",", ":")).encode("utf-8")
    headers = sign_native_webhook_headers(
        signing_secret=signing_secret,
        raw_body=raw_body,
        webhook_id=uuid.uuid4().hex,
    )
    headers["Content-Type"] = "application/json"

    url = f"{orchestra_url.rstrip('/')}{_NATIVE_GOOGLE_INGRESS_SUFFIX}"
    return requests.post(
        url,
        data=raw_body,
        headers=headers,
        timeout=_ORCHESTRA_POST_TIMEOUT_SECONDS,
    )
