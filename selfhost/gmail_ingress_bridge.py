#!/usr/bin/env python3
"""Bridge inbound Gmail into a local self-host CM, no public ingress required.

Internal developer tooling for the full local stack. The hosted comms layer
delivers inbound email via Gmail watch -> Pub/Sub, which a CM running on a
laptop cannot receive. This bridge instead polls a Workspace mailbox over the
Gmail API (using a domain-wide-delegation service account) and forwards each new
message to the CM's local ingress endpoint (``POST /local/comms/email``) — the
same envelope the in-process IMAP poller produces — so onboarding reply steps
advance exactly as they do hosted. Outbound send still flows through the gateway.

Nothing here is open-source ``droid`` code: it lives in the private
``droid-deploy`` repo and reads all credentials from the environment / a local
file, so no secret or internal address is ever committed.

Environment:
  GMAIL_BRIDGE_SA_FILE      Path to the service-account JSON (default
                            ``~/.droid/comms_sa.json``). Must have domain-wide
                            delegation for the Gmail scopes below.
  GMAIL_BRIDGE_MAILBOX      Mailbox to impersonate/poll (e.g.
                            ``staging-marty@unify.ai``). Required.
  GMAIL_BRIDGE_INGRESS_URL  CM local ingress base URL
                            (default ``http://127.0.0.1:8787``).
  GMAIL_BRIDGE_POLL_SECONDS Poll interval in seconds (default ``10``).
  GMAIL_BRIDGE_QUERY        Gmail search query for new mail
                            (default ``is:unread in:inbox``).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from email import message_from_bytes
from email.message import Message
from email.utils import getaddresses, make_msgid
from pathlib import Path

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _sa_file() -> str:
    return _env("GMAIL_BRIDGE_SA_FILE") or str(Path.home() / ".droid" / "comms_sa.json")


def _mailbox() -> str:
    address = _env("GMAIL_BRIDGE_MAILBOX")
    if not address:
        print("GMAIL_BRIDGE_MAILBOX must be set", file=sys.stderr)
        raise SystemExit(2)
    return address


def _ingress_url() -> str:
    return _env("GMAIL_BRIDGE_INGRESS_URL", "http://127.0.0.1:8787").rstrip("/")


def _gmail_service():
    info = json.loads(Path(_sa_file()).read_text(encoding="utf-8"))
    creds = Credentials.from_service_account_info(
        info,
        scopes=_SCOPES,
        subject=_mailbox(),
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _extract_body(message: Message) -> str:
    if message.is_multipart():
        for content_type in ("text/plain", "text/html"):
            for part in message.walk():
                if part.get_content_disposition() == "attachment":
                    continue
                if part.get_content_type() == content_type:
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return ""
    payload = message.get_payload(decode=True) or b""
    charset = message.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _attachments(message: Message) -> list[dict]:
    items: list[dict] = []
    for part in message.walk():
        if part.get_content_disposition() != "attachment":
            continue
        payload = part.get_payload(decode=True) or b""
        items.append(
            {
                "filename": part.get_filename() or "attachment",
                "content_base64": base64.b64encode(payload).decode("ascii"),
                "content_type": part.get_content_type(),
                "size_bytes": len(payload),
            },
        )
    return items


def _recipients(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return [addr for _, addr in getaddresses([header_value]) if addr]


def _envelope(message: Message) -> dict:
    return {
        "thread": "email",
        "event": {
            "from": message.get("From", ""),
            "subject": message.get("Subject", ""),
            "body": _extract_body(message),
            "email_id": message.get("Message-ID") or make_msgid(),
            "attachments": _attachments(message),
            "to": _recipients(message.get("To")),
            "cc": _recipients(message.get("Cc")),
            "bcc": _recipients(message.get("Bcc")),
        },
    }


def _poll_once(service, seen: set[str], since_ms: int) -> int:
    # Watermark by arrival time and dedup by id rather than relying on the UNREAD
    # label: replies may already be read, and (importantly) we never mutate the
    # shared mailbox — no marking read — so staging's view is untouched. The
    # ``after:`` epoch filter keeps the listing small; the precise cutoff is
    # enforced from each message's internalDate.
    base_query = _env("GMAIL_BRIDGE_QUERY", "in:inbox")
    query = f"{base_query} after:{since_ms // 1000}"
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
        raw = base64.urlsafe_b64decode(fetched["raw"].encode("ascii"))
        message = message_from_bytes(raw)
        envelope = _envelope(message)
        response = requests.post(
            f"{_ingress_url()}/local/comms/email",
            json=envelope,
            timeout=15,
        )
        response.raise_for_status()
        seen.add(gmail_id)
        delivered += 1
        sender = envelope["event"]["from"]
        subject = envelope["event"]["subject"]
        print(f"[bridge] delivered from={sender!r} subject={subject!r}", flush=True)
    return delivered


def main() -> None:
    mailbox = _mailbox()
    interval = float(_env("GMAIL_BRIDGE_POLL_SECONDS", "10"))
    lookback = float(_env("GMAIL_BRIDGE_LOOKBACK_SECONDS", "0"))
    since_ms = int((time.time() - lookback) * 1000)
    service = _gmail_service()
    profile = service.users().getProfile(userId="me").execute()
    print(
        f"[bridge] polling {profile.get('emailAddress')} "
        f"(total={profile.get('messagesTotal')}) -> {_ingress_url()} "
        f"every {interval:g}s; forwarding mail newer than "
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(since_ms / 1000))}",
        flush=True,
    )
    seen: set[str] = set()
    while True:
        try:
            _poll_once(service, seen, since_ms)
        except Exception as exc:  # keep the bridge alive across transient errors
            print(f"[bridge] poll error: {exc}", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
