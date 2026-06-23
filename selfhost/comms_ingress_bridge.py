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
open-source ``droid`` code: it lives in the private ``droid-deploy`` repo and
reads all credentials from the environment / local files under ``~/.droid``, so
no secret or internal address is ever committed.

Adapters & env:
  Gmail (email)
    GMAIL_BRIDGE_MAILBOX      Mailbox to impersonate/poll.
    GMAIL_BRIDGE_SA_FILE      SA JSON with Gmail domain-wide delegation
                              (default ``~/.droid/comms_sa.json``).
  Twilio (sms, whatsapp)
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN
    COMMS_BRIDGE_SMS_NUMBER       Coordinator SMS number (E.164).
    COMMS_BRIDGE_WHATSAPP_NUMBER  Coordinator WhatsApp number (E.164).
    COMMS_BRIDGE_TWILIO_ALLOWLIST Comma-separated sender numbers (E.164) the
                              bridge will forward. REQUIRED for Twilio: without
                              it the SMS/WhatsApp adapters no-op, so a dev CM
                              sharing a hosted number never reacts to (or
                              replies to) that deployment's real inbound.
  Common
    COMMS_BRIDGE_INGRESS_URL  CM local ingress base (default
                              ``http://127.0.0.1:8787``).
    COMMS_BRIDGE_POLL_SECONDS Poll interval (default ``10``).
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


# --------------------------------------------------------------------------- #
# Gmail adapter (email)
# --------------------------------------------------------------------------- #


class GmailAdapter:
    name = "email"

    def __init__(self) -> None:
        self._mailbox = _env("GMAIL_BRIDGE_MAILBOX")
        self._sa_file = _env("GMAIL_BRIDGE_SA_FILE") or str(
            Path.home() / ".droid" / "comms_sa.json",
        )
        self._service = None

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
        query = f"in:inbox after:{since_ms // 1000}"
        listing = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=25)
            .execute()
        )
        delivered = 0
        for entry in listing.get("messages", []):
            gmail_id = entry["id"]
            if gmail_id in seen:
                continue
            fetched = (
                service.users()
                .messages()
                .get(userId="me", id=gmail_id, format="raw")
                .execute()
            )
            if int(fetched.get("internalDate", "0")) < since_ms:
                seen.add(gmail_id)
                continue
            message = message_from_bytes(
                base64.urlsafe_b64decode(fetched["raw"].encode("ascii")),
            )
            _post("/local/comms/envelope", _email_envelope(message))
            seen.add(gmail_id)
            delivered += 1
            print(
                f"[bridge] email from={message.get('From','')!r} "
                f"subject={message.get('Subject','')!r}",
                flush=True,
            )
        return delivered


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


def _email_recipients(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return [addr for _, addr in getaddresses([header_value]) if addr]


def _email_envelope(message: Message) -> dict:
    return {
        "thread": "email",
        "event": {
            "from": message.get("From", ""),
            "subject": message.get("Subject", ""),
            "body": _email_body(message),
            "email_id": message.get("Message-ID") or make_msgid(),
            "attachments": [],
            "to": _email_recipients(message.get("To")),
            "cc": _email_recipients(message.get("Cc")),
            "bcc": _email_recipients(message.get("Bcc")),
        },
    }


# --------------------------------------------------------------------------- #
# Twilio adapter (sms / whatsapp)
# --------------------------------------------------------------------------- #


class TwilioAdapter:
    """Polls Twilio's Messages API for inbound texts on one channel.

    ``channel`` is ``"sms"`` (``thread: "msg"``) or ``"whatsapp"``. A sender
    allowlist is mandatory: when sharing a hosted number, this keeps the local
    CM from ingesting (and replying to) that deployment's real inbound.
    """

    def __init__(self, channel: str, number: str, allowlist: set[str]) -> None:
        self.name = channel
        self._channel = channel
        self._number = number
        self._allowlist = allowlist
        self._wa = channel == "whatsapp"
        self._client = None

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
        return bool(sid and tok and self._number and self._allowlist)

    def describe(self) -> str:
        return f"{self._channel}<{self._number}> allow={sorted(self._allowlist)}"

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
            if sender not in self._allowlist:
                continue
            _post("/local/comms/envelope", self._envelope(msg, sender))
            seen.add(msg.sid)
            delivered += 1
            print(
                f"[bridge] {self._channel} from={sender!r} body={ (msg.body or '')[:40]!r}",
                flush=True,
            )
        return delivered

    def _envelope(self, msg, sender: str) -> dict:
        thread = "whatsapp" if self._wa else "msg"
        return {
            "thread": thread,
            "event": {
                "contacts": [],
                "from_number": sender if self._wa else (msg.from_ or ""),
                "to_number": self._number if self._wa else (msg.to or ""),
                "body": msg.body or "",
                "attachments": [],
            },
        }


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


def _build_adapters() -> list:
    adapters: list = [GmailAdapter()]
    allowlist = {
        n.strip() for n in _env("COMMS_BRIDGE_TWILIO_ALLOWLIST").split(",") if n.strip()
    }
    twilio_configured = bool(_env("TWILIO_ACCOUNT_SID") and _env("TWILIO_AUTH_TOKEN"))
    if twilio_configured and not allowlist:
        print(
            "[bridge] WARNING: Twilio creds present but COMMS_BRIDGE_TWILIO_ALLOWLIST "
            "is empty — SMS/WhatsApp inbound disabled so the local CM cannot react "
            "to the shared number's real traffic. Set it to your test sender(s).",
            file=sys.stderr,
            flush=True,
        )
    adapters.append(TwilioAdapter("sms", _env("COMMS_BRIDGE_SMS_NUMBER"), allowlist))
    adapters.append(
        TwilioAdapter("whatsapp", _env("COMMS_BRIDGE_WHATSAPP_NUMBER"), allowlist),
    )
    return [a for a in adapters if a.configured()]


def main() -> None:
    interval = float(_env("COMMS_BRIDGE_POLL_SECONDS", "10"))
    lookback = float(_env("COMMS_BRIDGE_LOOKBACK_SECONDS", "0"))
    since_ms = int((time.time() - lookback) * 1000)
    adapters = _build_adapters()
    if not adapters:
        print("[bridge] no channels configured — nothing to poll; exiting", flush=True)
        return
    print(
        f"[bridge] polling {', '.join(a.describe() for a in adapters)} -> "
        f"{_ingress_url()} every {interval:g}s; forwarding items newer than "
        f"{datetime.fromtimestamp(since_ms / 1000)}",
        flush=True,
    )
    seen: dict[str, set[str]] = {a.name: set() for a in adapters}
    while True:
        for adapter in adapters:
            try:
                adapter.poll(since_ms, seen[adapter.name])
            except Exception as exc:  # keep the bridge alive across transient errors
                print(
                    f"[bridge] {adapter.name} poll error: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
        time.sleep(interval)


if __name__ == "__main__":
    main()
