#!/usr/bin/env python3
"""Bridge inbound hosted comms into a local self-host CM, no public webhook.

Internal developer tooling for the full local stack. The hosted comms layer
delivers inbound (email via Gmail watch -> Pub/Sub, SMS/WhatsApp via Twilio
webhooks) to endpoints a CM running on a laptop cannot receive. This bridge
instead *polls* each provider's API and forwards new inbound items to the CM's
local ingress, so the local Coordinator behaves as it would hosted. Outbound
send still flows through the gateway.

Channels are pluggable adapters; each is active only when its credentials are
configured, so the default fully-local stack is unaffected. Nothing here is
open-source ``unity`` code: it lives in the private ``unity-deploy`` repo and
reads all credentials from the environment / local files under ``~/.unity``, so
no secret or internal address is ever committed.

Adapters & env:
  Gmail (email)
    GMAIL_BRIDGE_MAILBOX      Mailbox to impersonate/poll.
    GMAIL_BRIDGE_SA_FILE      SA JSON with Gmail domain-wide delegation
                              (default ``~/.unity/comms_sa.json``).
  Twilio (sms, whatsapp)
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN
    COMMS_BRIDGE_SMS_NUMBER       Coordinator SMS number (E.164).
    COMMS_BRIDGE_WHATSAPP_NUMBER  Coordinator WhatsApp number (E.164).
    ORCHESTRA_URL / ORCHESTRA_ADMIN_KEY
                              Local Orchestra resolves ownership for each
                              inbound sender before anything reaches the CM.
  Common
    COMMS_BRIDGE_INGRESS_URL  CM local ingress base (default
                              ``http://127.0.0.1:8787``).
    COMMS_BRIDGE_POLL_SECONDS Poll interval (default ``10``).
    GMAIL_BRIDGE_POLL_SECONDS Gmail poll interval (default ``2``).
    COMMS_BRIDGE_LOOKBACK_SECONDS  Only forward items newer than now minus this
                              (default ``0`` = since bridge start).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import Message
from email.utils import getaddresses, make_msgid
from pathlib import Path

import requests


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _ingress_url() -> str:
    return _env("COMMS_BRIDGE_INGRESS_URL", "http://127.0.0.1:8787").rstrip("/")


def _post(path: str, envelope: dict) -> None:
    response = requests.post(f"{_ingress_url()}{path}", json=envelope, timeout=15)
    response.raise_for_status()


def _orchestra_base_url() -> str:
    return _env("ORCHESTRA_URL", "http://127.0.0.1:8000/v0").rstrip("/")


def _orchestra_admin_headers() -> dict[str, str] | None:
    admin_key = _env("ORCHESTRA_ADMIN_KEY")
    if not admin_key:
        return None
    return {"Authorization": f"Bearer {admin_key}"}


def _call_permission_status(button_payload: str) -> tuple[str, str]:
    payload = (button_payload or "").strip()
    if payload == "ACCEPTED":
        return "accepted", "ACCEPTED"
    if payload == "REJECTED":
        return "rejected", "REJECTED"
    return "unknown_interaction", "UNKNOWN"


def _permission_cache_path() -> Path:
    configured = _env("COMMS_BRIDGE_PERMISSION_CACHE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".unity" / "whatsapp_call_permissions.json"


def _permission_cache_key(pool_number: str, contact_number: str) -> str:
    return f"{pool_number}|{contact_number}"


def _load_permission_cache() -> dict[str, dict]:
    path = _permission_cache_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[bridge] failed to read WhatsApp permission cache: {exc}", flush=True)
        return {}
    if isinstance(payload, dict):
        return {
            str(key): value for key, value in payload.items() if isinstance(value, dict)
        }
    return {}


def _write_permission_cache(cache: dict[str, dict]) -> None:
    path = _permission_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _cache_call_permission(
    *,
    pool_number: str,
    contact_number: str,
    status: str,
    response_payload: dict | None,
) -> None:
    if status not in {"accepted", "rejected"}:
        return
    cache = _load_permission_cache()
    cache[_permission_cache_key(pool_number, contact_number)] = {
        "pool_number": pool_number,
        "contact_number": contact_number,
        "status": status,
        "expires_at": (response_payload or {}).get("expires_at"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_permission_cache(cache)


def _cached_permission_is_expired(entry: dict) -> bool:
    if entry.get("status") != "accepted":
        return False
    expires_at = entry.get("expires_at")
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    return parsed <= datetime.now(parsed.tzinfo)


def _hydrate_call_permission_cache() -> None:
    headers = _orchestra_admin_headers()
    if headers is None:
        return
    hydrated = 0
    for entry in _load_permission_cache().values():
        status = entry.get("status")
        pool_number = entry.get("pool_number")
        contact_number = entry.get("contact_number")
        if (
            status not in {"accepted", "rejected"}
            or not pool_number
            or not contact_number
        ):
            continue
        if _cached_permission_is_expired(entry):
            continue
        try:
            response = requests.post(
                f"{_orchestra_base_url()}/admin/whatsapp/call-permission",
                headers=headers,
                json={
                    "pool_number": pool_number,
                    "contact_number": contact_number,
                    "status": status,
                    "source": "selfhost_bridge_cache",
                },
                timeout=10,
            )
            response.raise_for_status()
            hydrated += 1
        except Exception as exc:
            print(
                "[bridge] whatsapp call permission cache hydration failed "
                f"for contact={contact_number!r}: {exc}",
                flush=True,
            )
    if hydrated:
        print(
            f"[bridge] hydrated {hydrated} WhatsApp call permission entr"
            f"{'y' if hydrated == 1 else 'ies'} from local cache",
            flush=True,
        )


# --------------------------------------------------------------------------- #
# Gmail adapter (email)
# --------------------------------------------------------------------------- #


class GmailAdapter:
    name = "email"

    def __init__(self) -> None:
        self._mailbox = _env("GMAIL_BRIDGE_MAILBOX")
        self._sa_file = _env("GMAIL_BRIDGE_SA_FILE") or str(
            Path.home() / ".unity" / "comms_sa.json",
        )
        self._service = None
        self._history_id: str | None = None
        self.poll_interval = float(_env("GMAIL_BRIDGE_POLL_SECONDS", "2"))

    def configured(self) -> bool:
        return bool(self._mailbox and Path(self._sa_file).is_file())

    def describe(self) -> str:
        return f"email<{self._mailbox}>"

    def _gmail(self):
        if self._service is None:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build

            info = json.loads(Path(self._sa_file).read_text(encoding="utf-8"))
            creds = Credentials.from_service_account_info(
                info,
                scopes=[
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/gmail.modify",
                ],
                subject=self._mailbox,
            )
            self._service = build(
                "gmail",
                "v1",
                credentials=creds,
                cache_discovery=False,
            )
        return self._service

    def poll(self, since_ms: int, seen: set[str]) -> int:
        service = self._gmail()
        if self._history_id is None:
            self._history_id = _gmail_current_history_id(service)
            return self._poll_unread_since(service, since_ms, seen)

        try:
            return self._poll_history(service, since_ms, seen)
        except Exception as exc:
            if _is_expired_gmail_history(exc):
                print(
                    "[bridge] Gmail history cursor expired; falling back to unread search",
                    file=sys.stderr,
                    flush=True,
                )
                self._history_id = _gmail_current_history_id(service)
                return self._poll_unread_since(service, since_ms, seen)
            raise

    def _poll_unread_since(self, service, since_ms: int, seen: set[str]) -> int:
        query = f"in:inbox is:unread after:{since_ms // 1000}"
        listing = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=25)
            .execute()
        )
        delivered = 0
        for entry in listing.get("messages", []):
            if self._process_gmail_message(service, entry["id"], since_ms, seen):
                delivered += 1
        return delivered

    def _poll_history(self, service, since_ms: int, seen: set[str]) -> int:
        delivered = 0
        page_token = None
        latest_history_id = self._history_id
        while True:
            kwargs = {
                "userId": "me",
                "startHistoryId": self._history_id,
                "historyTypes": ["messageAdded"],
            }
            if page_token:
                kwargs["pageToken"] = page_token
            request = service.users().history().list(**kwargs)
            response = request.execute()
            latest_history_id = response.get("historyId") or latest_history_id
            for history in response.get("history", []):
                for gmail_id in _history_message_ids(history):
                    if self._process_gmail_message(service, gmail_id, since_ms, seen):
                        delivered += 1
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        self._history_id = (
            str(latest_history_id) if latest_history_id else self._history_id
        )
        return delivered

    def _process_gmail_message(
        self,
        service,
        gmail_id: str,
        since_ms: int,
        seen: set[str],
    ) -> bool:
        if gmail_id in seen:
            return False
        fetched = (
            service.users()
            .messages()
            .get(userId="me", id=gmail_id, format="raw")
            .execute()
        )
        internal_ms = int(fetched.get("internalDate", "0"))
        if internal_ms < since_ms:
            seen.add(gmail_id)
            return False
        if "UNREAD" not in fetched.get("labelIds", []):
            seen.add(gmail_id)
            return False
        message = message_from_bytes(
            base64.urlsafe_b64decode(fetched["raw"].encode("ascii")),
        )
        seen.add(gmail_id)
        if _message_from_mailbox(message, self._mailbox):
            _mark_gmail_read(service, gmail_id)
            print(
                f"[bridge] skipped self email subject={message.get('Subject','')!r}",
                flush=True,
            )
            return False
        _post(
            "/local/comms/envelope",
            _email_envelope(
                message,
                internal_ms,
                gmail_message_id=gmail_id,
                thread_id=fetched.get("threadId"),
            ),
        )
        _mark_gmail_read(service, gmail_id)
        print(
            f"[bridge] email from={message.get('From','')!r} "
            f"subject={message.get('Subject','')!r}",
            flush=True,
        )
        return True


def _email_body(message: Message) -> str:
    if message.is_multipart():
        for content_type in ("text/plain", "text/html"):
            for part in message.walk():
                if part.get_content_disposition() == "attachment":
                    continue
                if part.get_content_type() == content_type:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(
                        part.get_content_charset() or "utf-8",
                        "replace",
                    )
        return ""
    payload = message.get_payload(decode=True) or b""
    return payload.decode(message.get_content_charset() or "utf-8", "replace")


def _mark_gmail_read(service, gmail_id: str) -> None:
    service.users().messages().modify(
        userId="me",
        id=gmail_id,
        body={"removeLabelIds": ["UNREAD"]},
    ).execute()


def _gmail_current_history_id(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return str(profile.get("historyId", ""))


def _history_message_ids(history: dict) -> list[str]:
    ids: list[str] = []
    for entry in history.get("messagesAdded", []):
        message = entry.get("message", {})
        gmail_id = message.get("id")
        if gmail_id:
            ids.append(gmail_id)
    for message in history.get("messages", []):
        gmail_id = message.get("id")
        if gmail_id:
            ids.append(gmail_id)
    return ids


def _is_expired_gmail_history(exc: Exception) -> bool:
    response = getattr(exc, "resp", None) or getattr(exc, "response", None)
    status = getattr(response, "status", None) or getattr(response, "status_code", None)
    return int(status or 0) in {400, 404}


def _email_recipients(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return [addr for _, addr in getaddresses([header_value]) if addr]


def _message_from_mailbox(message: Message, mailbox: str) -> bool:
    mailbox_lower = mailbox.strip().lower()
    if not mailbox_lower:
        return False
    return any(
        addr.lower() == mailbox_lower for addr in _email_recipients(message.get("From"))
    )


def _email_envelope(
    message: Message,
    internal_ms: int | None = None,
    *,
    gmail_message_id: str | None = None,
    thread_id: str | None = None,
) -> dict:
    envelope = {
        "thread": "email",
        "event": {
            "from": message.get("From", ""),
            "subject": message.get("Subject", ""),
            "body": _email_body(message),
            "email_id": message.get("Message-ID") or make_msgid(),
            "thread_id": thread_id or "",
            "gmail_message_id": gmail_message_id or "",
            "attachments": [],
            "to": _email_recipients(message.get("To")),
            "cc": _email_recipients(message.get("Cc")),
            "bcc": _email_recipients(message.get("Bcc")),
        },
    }
    if internal_ms is not None:
        envelope["publish_timestamp"] = internal_ms / 1000
    return envelope


# --------------------------------------------------------------------------- #
# Twilio adapter (sms / whatsapp)
# --------------------------------------------------------------------------- #


class TwilioAdapter:
    """Polls Twilio's Messages API for inbound texts on one channel.

    ``channel`` is ``"sms"`` (``thread: "msg"``) or ``"whatsapp"``. Twilio is
    polled locally, but ownership/routing stays with local Orchestra via the
    same resolve endpoints used by hosted adapters.
    """

    def __init__(self, channel: str, number: str) -> None:
        self.name = channel
        self._channel = channel
        self._number = number
        self._wa = channel == "whatsapp"
        self._client = None
        self.poll_interval = float(_env("COMMS_BRIDGE_POLL_SECONDS", "10"))

    def _creds(self) -> tuple[str, str]:
        # WhatsApp is a distinct Twilio account from SMS/voice; fall back to the
        # main account only when no dedicated WA creds are configured.
        if self._wa:
            sid = _env("TWILIO_WA_ACCOUNT_SID") or _env("TWILIO_ACCOUNT_SID")
            tok = _env("TWILIO_WA_AUTH_TOKEN") or _env("TWILIO_AUTH_TOKEN")
            return sid, tok
        return _env("TWILIO_ACCOUNT_SID"), _env("TWILIO_AUTH_TOKEN")

    def configured(self) -> bool:
        sid, tok = self._creds()
        return bool(sid and tok and self._number)

    def describe(self) -> str:
        return f"{self._channel}<{self._number}>"

    def _twilio_number(self) -> str:
        return f"whatsapp:{self._number}" if self._wa else self._number

    def _client_(self):
        if self._client is None:
            from twilio.rest import Client

            sid, tok = self._creds()
            self._client = Client(sid, tok)
        return self._client

    @staticmethod
    def _strip(number: str) -> str:
        return number.replace("whatsapp:", "").strip()

    def poll(self, since_ms: int, seen: set[str]) -> int:
        client = self._client_()
        since_dt = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc)
        # Twilio filters date_sent at day granularity; widen by a day and refine
        # against each message's precise timestamp below.
        messages = client.messages.list(
            to=self._twilio_number(),
            date_sent_after=since_dt - timedelta(days=1),
            limit=50,
        )
        delivered = 0
        for msg in messages:
            if msg.sid in seen:
                continue
            if getattr(msg, "direction", "") != "inbound":
                continue
            sent = msg.date_sent or msg.date_created
            if sent is not None:
                sent_utc = sent if sent.tzinfo else sent.replace(tzinfo=timezone.utc)
                if sent_utc.timestamp() * 1000 < since_ms:
                    seen.add(msg.sid)
                    continue
            sender = self._strip(msg.from_ or "")
            route = self._resolve_route(sender)
            if route is None:
                seen.add(msg.sid)
                continue
            if self._wa and self._is_call_permission_response(msg):
                envelope = self._call_permission_envelope(msg, sender, route)
                self._record_call_permission(sender, msg)
            else:
                envelope = self._envelope(msg, sender, route)
            _post("/local/comms/envelope", envelope)
            seen.add(msg.sid)
            delivered += 1
            print(
                f"[bridge] {self._channel} from={sender!r} body={ (msg.body or '')[:40]!r}",
                flush=True,
            )
        return delivered

    @staticmethod
    def _is_call_permission_response(msg) -> bool:
        return (getattr(msg, "body", "") or "").strip() == "VOICE_CALL_REQUEST"

    @staticmethod
    def _button_payload(msg) -> str:
        payload = getattr(msg, "button_payload", "") or getattr(
            msg,
            "ButtonPayload",
            "",
        )
        return str(payload or "").strip()

    def _resolve_route(self, sender: str) -> dict | None:
        headers = _orchestra_admin_headers()
        if headers is None:
            print(
                f"[bridge] {self._channel} from={sender!r} skipped: ORCHESTRA_ADMIN_KEY missing",
                flush=True,
            )
            return None

        platform = "whatsapp" if self._wa else "phone"
        try:
            response = requests.get(
                f"{_orchestra_base_url()}/admin/{platform}/resolve",
                params={"pool_number": self._number, "sender": sender},
                headers=headers,
                timeout=10,
            )
        except Exception as exc:
            print(
                f"[bridge] {self._channel} resolve failed for from={sender!r}: {exc}",
                flush=True,
            )
            return None

        if response.status_code == 404:
            print(
                f"[bridge] {self._channel} from={sender!r} skipped: no local route",
                flush=True,
            )
            return None

        try:
            response.raise_for_status()
            route = response.json()
        except Exception as exc:
            print(
                f"[bridge] {self._channel} resolve failed for from={sender!r}: {exc}",
                flush=True,
            )
            return None

        action = route.get("action")
        if action:
            print(
                f"[bridge] {self._channel} from={sender!r} skipped: action={action}",
                flush=True,
            )
            return None

        assistant_id = route.get("assistant_id")
        if assistant_id is None:
            print(
                f"[bridge] {self._channel} from={sender!r} skipped: missing assistant_id",
                flush=True,
            )
            return None

        return {
            "assistant_id": assistant_id,
            "role": route.get("role") or "contact",
        }

    def _call_permission_envelope(self, msg, sender: str, route: dict) -> dict:
        button_payload = self._button_payload(msg)
        _, event_payload = _call_permission_status(button_payload)
        return {
            "thread": "whatsapp",
            "event": {
                "contacts": [],
                "type": "call_permission_response",
                "contact_number": sender,
                "from_number": sender,
                "to_number": self._number,
                "body": msg.body or "",
                "payload": event_payload,
                "attachments": [],
                "assistant_id": route["assistant_id"],
                "role": route["role"],
            },
        }

    def _record_call_permission(self, sender: str, msg) -> None:
        headers = _orchestra_admin_headers()
        if headers is None:
            return
        button_payload = self._button_payload(msg)
        status, _ = _call_permission_status(button_payload)
        try:
            response = requests.post(
                f"{_orchestra_base_url()}/admin/whatsapp/call-permission",
                headers=headers,
                json={
                    "pool_number": self._number,
                    "contact_number": sender,
                    "status": status,
                    "source": "selfhost_bridge",
                },
                timeout=10,
            )
            response.raise_for_status()
            try:
                response_payload = response.json()
            except Exception:
                response_payload = None
            _cache_call_permission(
                pool_number=self._number,
                contact_number=sender,
                status=status,
                response_payload=response_payload,
            )
            print(
                f"[bridge] whatsapp call permission {status} from={sender!r}",
                flush=True,
            )
        except Exception as exc:
            print(f"[bridge] whatsapp call permission update failed: {exc}", flush=True)

    def _envelope(self, msg, sender: str, route: dict) -> dict:
        thread = "whatsapp" if self._wa else "msg"
        return {
            "thread": thread,
            "event": {
                "contacts": [],
                "from_number": sender if self._wa else (msg.from_ or ""),
                "to_number": self._number if self._wa else (msg.to or ""),
                "body": msg.body or "",
                "attachments": [],
                "assistant_id": route["assistant_id"],
                "role": route["role"],
            },
        }


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def _build_adapters() -> list:
    adapters: list = [GmailAdapter()]
    adapters.append(TwilioAdapter("sms", _env("COMMS_BRIDGE_SMS_NUMBER")))
    adapters.append(TwilioAdapter("whatsapp", _env("COMMS_BRIDGE_WHATSAPP_NUMBER")))
    return [a for a in adapters if a.configured()]


def main() -> None:
    lookback = float(_env("COMMS_BRIDGE_LOOKBACK_SECONDS", "0"))
    since_ms = int((time.time() - lookback) * 1000)
    adapters = _build_adapters()
    if not adapters:
        print("[bridge] no channels configured — nothing to poll; exiting", flush=True)
        return
    _hydrate_call_permission_cache()
    print(
        f"[bridge] polling {', '.join(a.describe() for a in adapters)} -> "
        f"{_ingress_url()}; forwarding items newer than "
        f"{datetime.fromtimestamp(since_ms / 1000)}",
        flush=True,
    )
    seen: dict[str, set[str]] = {a.name: set() for a in adapters}
    next_poll_at: dict[str, float] = {a.name: 0 for a in adapters}
    while True:
        now = time.time()
        for adapter in adapters:
            if now < next_poll_at[adapter.name]:
                continue
            try:
                adapter.poll(since_ms, seen[adapter.name])
            except Exception as exc:  # keep the bridge alive across transient errors
                print(
                    f"[bridge] {adapter.name} poll error: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                next_poll_at[adapter.name] = now + getattr(adapter, "poll_interval", 10)
        sleep_for = min(
            max(next_poll_at[name] - time.time(), 0.25) for name in next_poll_at
        )
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
