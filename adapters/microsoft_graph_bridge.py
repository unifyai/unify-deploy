"""Bridge Microsoft Graph change notifications into Orchestra ingress.

Graph delivers change and lifecycle notifications to Adapters over HTTPS.
This module owns the translation from that Graph webhook payload into a
Unify-shaped, HMAC-signed request that Orchestra's ``native_microsoft``
webhook ingress accepts.

Responsibilities, kept out of the request handler so they stay unit-testable:

- echo Graph ``validationToken`` challenges as plain text;
- verify ``clientState`` against the shared native Microsoft secret;
- map each notification to retry-stable ``event_id``,
  ``provider_trigger_slug`` (from resource + changeType), and
  ``external_trigger_id`` = Graph ``subscriptionId``;
- sign the outbound body with ``NATIVE_MICROSOFT_WEBHOOK_SECRET`` using the
  same scheme Orchestra verifies;
- POST it to the unrouted ``native_microsoft`` ingress path so Orchestra
  resolves the binding by ``external_trigger_id``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

import requests

from .workspace_events_bridge import sign_native_webhook_headers

logger = logging.getLogger(__name__)

_NATIVE_MICROSOFT_INGRESS_SUFFIX = "/webhooks/integrations/native_microsoft"
_ORCHESTRA_POST_TIMEOUT_SECONDS = 10
_GRAPH_CLIENT_STATE_MAX_LEN = 128

# Graph resource suffixes (after optional users/.../ prefix) + changeType →
# curated native Microsoft provider_trigger_slug. Keep aligned with
# orchestra's native_microsoft_graph_subscriptions.json delegated entries.
_RESOURCE_CHANGE_TO_SLUG: tuple[tuple[str, str, str], ...] = (
    ("communications/onlineMeetings/", "created", "microsoft.graph.callTranscript.created.meeting"),
    ("me/messages", "created", "microsoft.graph.mailMessage.created"),
    ("me/messages", "updated", "microsoft.graph.mailMessage.updated"),
    ("/messages", "created", "microsoft.graph.mailMessage.created"),
    ("/messages", "updated", "microsoft.graph.mailMessage.updated"),
    ("me/events", "created", "microsoft.graph.event.created"),
    ("me/events", "updated", "microsoft.graph.event.updated"),
    ("me/events", "deleted", "microsoft.graph.event.deleted"),
    ("/events", "created", "microsoft.graph.event.created"),
    ("/events", "updated", "microsoft.graph.event.updated"),
    ("/events", "deleted", "microsoft.graph.event.deleted"),
    ("me/drive/root", "updated", "microsoft.graph.driveItem.updated"),
    ("/drive/root", "updated", "microsoft.graph.driveItem.updated"),
    ("me/contacts", "created", "microsoft.graph.contact.created"),
    ("/contacts", "created", "microsoft.graph.contact.created"),
    ("/todo/lists/", "created", "microsoft.graph.todoTask.created"),
)


class MicrosoftGraphBridgeAuthError(Exception):
    """The Graph notification failed clientState verification."""


class MicrosoftGraphBridgeMappingError(Exception):
    """The Graph notification was not mappable to a native delivery."""


@dataclass(frozen=True)
class NativeMicrosoftDelivery:
    """One Graph notification mapped to the Orchestra native_microsoft body."""

    event_id: str
    external_trigger_id: str
    payload: dict[str, Any]


def graph_client_state(webhook_secret: str) -> str:
    """Return the Graph ``clientState`` value derived from the shared secret."""

    secret = webhook_secret.strip()
    if not secret:
        raise ValueError("native Microsoft webhook secret is required for clientState")
    if len(secret) <= _GRAPH_CLIENT_STATE_MAX_LEN:
        return secret
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_graph_client_state(
    notification: Mapping[str, Any],
    *,
    webhook_secret: str,
) -> None:
    """Raise when notification ``clientState`` does not match the shared secret."""

    expected = graph_client_state(webhook_secret)
    actual = str(notification.get("clientState") or "")
    if not actual or not hmac.compare_digest(actual, expected):
        raise MicrosoftGraphBridgeAuthError("Graph clientState mismatch")


def _normalize_resource(resource: str) -> str:
    value = resource.strip()
    # Graph may deliver ``Users/{id}/Messages/{id}`` or ``me/messages``.
    lower = value.lower()
    return lower


def resolve_provider_trigger_slug(
    *,
    resource: str,
    change_type: str,
    lifecycle_event: str | None = None,
) -> str:
    """Map a Graph notification onto a curated native Microsoft slug."""

    if lifecycle_event:
        return "microsoft.graph.lifecycle"
    resource_norm = _normalize_resource(resource)
    change = change_type.strip().lower()
    for suffix, expected_change, slug in _RESOURCE_CHANGE_TO_SLUG:
        if expected_change == change and suffix.lower() in resource_norm:
            return slug
    raise MicrosoftGraphBridgeMappingError(
        f"unsupported Graph resource/changeType: {resource!r} / {change_type!r}",
    )


def _stable_event_id(notification: Mapping[str, Any]) -> str:
    """Build a retry-stable identity for one Graph notification.

    Prefer Graph's per-notification ``id`` when present. Otherwise combine
    subscription, change type, resource id, and ``@odata.etag`` so repeated
    updates to the same item remain distinct.
    """

    notification_id = str(notification.get("id") or "").strip()
    if notification_id:
        return notification_id

    subscription_id = str(notification.get("subscriptionId") or "").strip()
    change_type = str(notification.get("changeType") or "").strip()
    lifecycle_event = str(notification.get("lifecycleEvent") or "").strip()
    resource = str(notification.get("resource") or "").strip()
    resource_data = notification.get("resourceData")
    resource_id = ""
    etag = ""
    if isinstance(resource_data, Mapping):
        resource_id = str(resource_data.get("id") or "").strip()
        etag = str(resource_data.get("@odata.etag") or "").strip()
    if lifecycle_event:
        parts = ["lifecycle", subscription_id, lifecycle_event]
    else:
        parts = [subscription_id, change_type, resource_id or resource, etag]
    event_id = ":".join(part for part in parts if part)
    if not event_id:
        raise MicrosoftGraphBridgeMappingError(
            "Graph notification is missing retry-stable identity fields",
        )
    return event_id


def map_graph_notification_to_delivery(
    notification: Mapping[str, Any],
) -> NativeMicrosoftDelivery:
    """Map one Graph change/lifecycle notification to a native_microsoft body."""

    if not isinstance(notification, Mapping):
        raise MicrosoftGraphBridgeMappingError("notification is not a JSON object")

    subscription_id = str(notification.get("subscriptionId") or "").strip()
    if not subscription_id:
        raise MicrosoftGraphBridgeMappingError(
            "Graph notification is missing subscriptionId",
        )

    lifecycle_event = str(notification.get("lifecycleEvent") or "").strip() or None
    change_type = str(notification.get("changeType") or "").strip()
    resource = str(notification.get("resource") or "").strip()
    if not lifecycle_event and (not change_type or not resource):
        raise MicrosoftGraphBridgeMappingError(
            "Graph notification is missing changeType/resource",
        )

    slug = resolve_provider_trigger_slug(
        resource=resource,
        change_type=change_type,
        lifecycle_event=lifecycle_event,
    )
    event_id = _stable_event_id(notification)

    data: dict[str, Any] = {
        "subscription_id": subscription_id,
        "resource": resource,
        "change_type": change_type,
        "event_id": event_id,
    }
    if lifecycle_event:
        data["lifecycle_event"] = lifecycle_event
    resource_data = notification.get("resourceData")
    if isinstance(resource_data, Mapping):
        data["resource_data"] = dict(resource_data)
        resource_id = resource_data.get("id")
        if isinstance(resource_id, str) and resource_id.strip():
            data["resource_id"] = resource_id.strip()
    tenant_id = notification.get("tenantId")
    if isinstance(tenant_id, str) and tenant_id.strip():
        data["tenant_id"] = tenant_id.strip()

    payload: dict[str, Any] = {
        "event_id": event_id,
        "provider_trigger_slug": slug,
        "external_trigger_id": subscription_id,
        "data": data,
    }
    if lifecycle_event:
        payload["lifecycle_event"] = lifecycle_event

    return NativeMicrosoftDelivery(
        event_id=event_id,
        external_trigger_id=subscription_id,
        payload=payload,
    )


def post_native_microsoft_delivery(
    delivery: NativeMicrosoftDelivery,
    *,
    orchestra_url: str,
    signing_secret: str,
) -> requests.Response:
    """Sign and POST a mapped delivery to Orchestra's native_microsoft ingress."""

    raw_body = json.dumps(delivery.payload, separators=(",", ":")).encode("utf-8")
    headers = sign_native_webhook_headers(
        signing_secret=signing_secret,
        raw_body=raw_body,
        webhook_id=uuid.uuid4().hex,
    )
    headers["Content-Type"] = "application/json"
    url = f"{orchestra_url.rstrip('/')}{_NATIVE_MICROSOFT_INGRESS_SUFFIX}"
    return requests.post(
        url,
        data=raw_body,
        headers=headers,
        timeout=_ORCHESTRA_POST_TIMEOUT_SECONDS,
    )
